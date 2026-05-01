"""Polymarket quote manager — order lifecycle (mirrors lip-maker quote_manager).

Tracks intended quotes per market, reconciles against live state via SDK.
Paper mode uses orders.preview (server validates, no money moves).
Live mode uses orders.create (real orders).

Key methods:
  reconcile(slug, target_yes, target_no)  — bring resting to match target
  cancel_all(slug=None)                    — cancel everything or per-market
  cold_boot_reconcile()                    — pull live orders on startup
  summary()                                — current state for dashboard
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from polymarket_us import PolymarketUS

from config import settings

_log = logging.getLogger(__name__)


@dataclass
class QuoteTarget:
    """Our desired quote on one market."""
    slug:        str
    yes_price:   Optional[float]  # None = no yes quote
    no_price:    Optional[float]  # None = no no quote
    quantity:    int


@dataclass
class RestingOrder:
    """A live resting order at Polymarket."""
    order_id:    str
    slug:        str
    intent:      str       # ORDER_INTENT_BUY_LONG | BUY_SHORT
    price:       float
    quantity:    int
    placed_at:   float


class PMQuoteManager:
    """Order lifecycle for Polymarket markets."""

    def __init__(self, client: PolymarketUS, paper: bool = True):
        self.client = client
        self.paper = paper
        # ticker → list of resting orders (max 1 yes + 1 no per market)
        self.resting: dict[str, list[RestingOrder]] = {}
        # Per-market last reconcile timestamp (rate-limit guard)
        self._last_reconcile_ts: dict[str, float] = {}
        # Cold-boot: pull live orders if not paper mode
        if not paper:
            self._cold_boot_reconcile()

    def _cold_boot_reconcile(self) -> None:
        """Fetch live resting orders from Polymarket → hydrate self.resting."""
        try:
            r = self.client.orders.list({"limit": 200})
            orders = r.get("orders", []) if isinstance(r, dict) else []
            for o in orders:
                slug = o.get("marketSlug")
                oid  = o.get("id")
                price = float(o.get("price", {}).get("value", 0)) if isinstance(o.get("price"), dict) else 0
                qty = int(float(o.get("leavesQuantity", o.get("quantity", 0))))
                if not (slug and oid and qty > 0):
                    continue
                ro = RestingOrder(
                    order_id=oid, slug=slug,
                    intent=o.get("intent", ""),
                    price=price, quantity=qty,
                    placed_at=time.time(),
                )
                self.resting.setdefault(slug, []).append(ro)
            _log.info(f"cold-boot: rehydrated {sum(len(v) for v in self.resting.values())} "
                      f"orders across {len(self.resting)} markets")
        except Exception as e:
            _log.warning(f"cold-boot failed: {e}")

    def reconcile_against_account(self) -> dict:
        """Sync self.resting against truth from Polymarket account state.

        AUDIT FIX 2026-04-29 (RED #1): without this, partial fills + reprice
        cycles cause self.resting to drift above stated caps. Run every
        cycle BEFORE planning new orders.

        Paper mode: clears self.resting (fresh preview each cycle —
        no real state to track).

        Live mode: queries /v1/orders/open, removes any memory entries
        not in live set (purged = filled or cancelled by Kalshi-side).
        """
        if self.paper:
            n_dropped = sum(len(v) for v in self.resting.values())
            self.resting.clear()
            return {"mode": "paper", "dropped": n_dropped}
        try:
            r = self.client.orders.list({"limit": 200})
            orders = r.get("orders", []) if isinstance(r, dict) else []
            live_ids: set[str] = set()
            for o in orders:
                oid = o.get("id")
                if oid:
                    live_ids.add(oid)
            phantoms = 0
            for slug, lst in list(self.resting.items()):
                kept = [o for o in lst if o.order_id in live_ids]
                phantoms += len(lst) - len(kept)
                if kept:
                    self.resting[slug] = kept
                else:
                    self.resting.pop(slug, None)
            if phantoms > 0:
                _log.info(f"reconcile_against_account: purged {phantoms} phantoms "
                          f"({len(live_ids)} live orders confirmed)")
            return {"mode": "live", "phantoms_purged": phantoms,
                    "live_count": len(live_ids)}
        except Exception as e:
            _log.warning(f"reconcile_against_account failed: {e}")
            return {"error": str(e)}

    def _virtual_post_only_check(self, slug: str, intent: str,
                                  price: float) -> tuple[bool, str]:
        """NEXUS-OMNI V4 D1: pre-flight book check ('Virtual Post-Only').
        PM US SDK doesn't expose post_only flag. We approximate by fetching
        live BBO and aborting if our quote would cross. Returns (ok, reason).

        Logic:
          BUY_LONG (buy YES) at $X — abort if best YES ask <= $X (we'd cross
            into the ask = pay taker fee, no rebate).
          BUY_SHORT (buy NO) at $Y on PM = sell YES at (1-Y).
            sell YES at (1-Y) — abort if best YES bid >= (1-Y) (we'd cross).
        """
        try:
            r = self.client.markets.bbo(slug)
            md = r.get("marketData", {}) if isinstance(r, dict) else {}
            best_bid = md.get("bestBid", {}).get("value")
            best_ask = md.get("bestAsk", {}).get("value")
            if best_bid is None or best_ask is None:
                return True, "no bbo (allow)"
            best_bid = float(best_bid)
            best_ask = float(best_ask)
            if "BUY_LONG" in intent:
                # Buying YES: abort if our bid >= best ask (would cross)
                if price >= best_ask:
                    return False, f"shielded: BUY_LONG ${price:.3f} >= best_ask ${best_ask:.3f}"
            elif "BUY_SHORT" in intent:
                # Selling YES at (1-our_no_price): abort if best_bid >= our YES sell price
                yes_sell_price = 1.0 - price
                if best_bid >= yes_sell_price:
                    return False, f"shielded: BUY_SHORT yes_sell ${yes_sell_price:.3f} <= best_bid ${best_bid:.3f}"
            return True, "ok"
        except Exception as e:
            # Don't block on infra issue; trust the cycle's already-fetched book
            return True, f"shield-skip ({str(e)[:40]})"

    def _place_order(self, slug: str, intent: str,
                     price: float, quantity: int) -> Optional[RestingOrder]:
        """Place one order. Paper mode → preview. Live mode → create."""
        # NEXUS-OMNI V4 D1: virtual post-only — abort if would cross
        if not self.paper:
            ok, reason = self._virtual_post_only_check(slug, intent, price)
            if not ok:
                _log.info(f"[SHIELD] {slug[:40]} {intent} @${price:.3f} → {reason}")
                return None

        body = {
            "marketSlug": slug,
            "intent":     intent,
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": f"{price:.3f}", "currency": "USD"},
            "quantity":   int(quantity),
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        if self.paper:
            try:
                resp = self.client.orders.preview({"request": body})
                # paper: synthesize an order_id; not a real Polymarket id
                fake_id = f"PAPER-{int(time.time()*1000)}-{slug[:20]}"
                _log.info(f"[PAPER] PLACE {slug} {intent} @${price:.3f} x{quantity}")
                ro = RestingOrder(order_id=fake_id, slug=slug, intent=intent,
                                  price=price, quantity=quantity,
                                  placed_at=time.time())
                self.resting.setdefault(slug, []).append(ro)
                return ro
            except Exception as e:
                _log.warning(f"[PAPER] preview failed {slug}: {str(e)[:120]}")
                return None
        # LIVE
        try:
            resp = self.client.orders.create(body)
            # SDK returns flat {'id': '9PWxxx', 'executions': [...]}
            # (some response variants nest under 'order' — try both).
            if isinstance(resp, dict):
                oid = resp.get("id") or resp.get("order", {}).get("id", "")
            else:
                oid = ""
            if not oid:
                _log.warning(f"[LIVE] PLACE returned no id: {resp}")
                return None
            _log.info(f"[LIVE] PLACED {slug} {intent} @${price:.3f} x{quantity} id={oid}")
            ro = RestingOrder(order_id=oid, slug=slug, intent=intent,
                              price=price, quantity=quantity,
                              placed_at=time.time())
            self.resting.setdefault(slug, []).append(ro)
            return ro
        except Exception as e:
            _log.warning(f"[LIVE] place failed {slug}: {str(e)[:200]}")
            return None

    def _cancel_order(self, order: RestingOrder) -> bool:
        if self.paper:
            _log.info(f"[PAPER] CANCEL {order.slug} {order.intent} @${order.price:.3f}")
        else:
            try:
                # SDK requires marketSlug in CancelOrderParams TypedDict
                self.client.orders.cancel(order.order_id, {"marketSlug": order.slug})
                _log.info(f"[LIVE] CANCELLED {order.slug} id={order.order_id}")
            except Exception as e:
                msg = str(e)
                if "Not Found" in msg or "404" in msg:
                    # Already gone — purge from memory anyway
                    _log.info(f"[LIVE] cancel-404-purge {order.order_id}")
                else:
                    _log.warning(f"[LIVE] cancel failed {order.order_id}: {msg[:120]}")
                    return False
        # Remove from memory
        lst = self.resting.get(order.slug, [])
        try:
            lst.remove(order)
        except ValueError:
            pass
        return True

    def _existing_for_intent(self, slug: str, intent: str) -> Optional[RestingOrder]:
        for o in self.resting.get(slug, []):
            if o.intent == intent:
                return o
        return None

    def reconcile(self, target: QuoteTarget) -> dict:
        """Bring our orders for `slug` in line with target."""
        # Throttle per-market reconciles to 1/sec to avoid spam
        now = time.time()
        last = self._last_reconcile_ts.get(target.slug, 0)
        if now - last < 1.0:
            return {"action": "throttled"}
        self._last_reconcile_ts[target.slug] = now

        actions = {"placed": 0, "cancelled": 0, "kept": 0}

        # YES side reconciliation
        existing_yes = self._existing_for_intent(target.slug, "ORDER_INTENT_BUY_LONG")
        if target.yes_price is None:
            if existing_yes and self._cancel_order(existing_yes):
                actions["cancelled"] += 1
        else:
            need_replace = (
                existing_yes is None
                or abs(existing_yes.price - target.yes_price) > 0.001
                or existing_yes.quantity != target.quantity
            )
            if need_replace:
                if existing_yes:
                    if self._cancel_order(existing_yes):
                        actions["cancelled"] += 1
                if self._place_order(target.slug, "ORDER_INTENT_BUY_LONG",
                                     target.yes_price, target.quantity):
                    actions["placed"] += 1
            else:
                actions["kept"] += 1

        # NO side reconciliation
        existing_no = self._existing_for_intent(target.slug, "ORDER_INTENT_BUY_SHORT")
        if target.no_price is None:
            if existing_no and self._cancel_order(existing_no):
                actions["cancelled"] += 1
        else:
            need_replace = (
                existing_no is None
                or abs(existing_no.price - target.no_price) > 0.001
                or existing_no.quantity != target.quantity
            )
            if need_replace:
                if existing_no:
                    if self._cancel_order(existing_no):
                        actions["cancelled"] += 1
                if self._place_order(target.slug, "ORDER_INTENT_BUY_SHORT",
                                     target.no_price, target.quantity):
                    actions["placed"] += 1
            else:
                actions["kept"] += 1

        return actions

    def cancel_all(self, slug: Optional[str] = None) -> int:
        """Cancel everything, optionally scoped.

        Live mode without a slug filter ALSO calls SDK orders.cancel_all()
        as a panic-belt — guarantees no phantom orders survive even if
        self.resting got out of sync (e.g. parser bug, crash, restart)."""
        if slug:
            orders = list(self.resting.get(slug, []))
        else:
            orders = [o for lst in self.resting.values() for o in lst]
        n = 0
        for o in orders:
            if self._cancel_order(o):
                n += 1
        # Panic belt: in live mode, sweep the venue too
        if not slug and not self.paper:
            try:
                r = self.client.orders.cancel_all()
                ids = r.get("canceledOrderIds", []) if isinstance(r, dict) else []
                if ids:
                    _log.warning(f"[LIVE] panic cancel_all swept {len(ids)} extra")
                    n += len(ids)
            except Exception as e:
                _log.error(f"[LIVE] panic cancel_all failed: {e}")
        return n

    def summary(self) -> dict:
        return {
            "mode":       "paper" if self.paper else "live",
            "n_markets":  len(self.resting),
            "n_orders":   sum(len(v) for v in self.resting.values()),
            "gross_usd":  sum(o.price * o.quantity for lst in self.resting.values() for o in lst),
        }
