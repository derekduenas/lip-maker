"""Quote manager — places/cancels resting two-sided LIP-eligible limit orders.

Given a target quote state per market (yes_bid, no_bid, size), reconciles
against actual resting orders. PAPER mode logs intent without hitting
Kalshi; LIVE mode places real orders with inventory-risk controls.

Safety layers (must all pass before placing a live order):
  1. Authenticated Kalshi client
  2. Market is enrolled (in lip_programs with enrolled=1)
  3. Per-market gross exposure ≤ MAX_GROSS_PER_MARKET_USD
  4. Per-market net inventory ≤ MAX_NET_INVENTORY_USD
  5. Total gross across all markets ≤ MAX_TOTAL_GROSS_USD
  6. Bankroll share ≤ MAX_BANKROLL_SHARE_PCT of current Kalshi balance
  7. Order size ≥ MIN_QUOTE_SIZE_CONTRACTS
  8. Spread (yes_bid → no_bid implied ask) ≤ MAX_SPREAD_CENTS

Kalshi order endpoint (POST /portfolio/orders) body:
  {
    "ticker":   "KXHIGH-...",
    "side":     "yes" | "no",
    "action":   "buy",
    "type":     "limit",
    "count":    number_of_contracts,
    "yes_price": cents,   # OR no_price: cents (must match `side`)
    "client_order_id": unique_id,
  }
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_auth import KalshiClient, KalshiAuthError

_log = logging.getLogger(__name__)


@dataclass
class QuoteTarget:
    """Our desired resting state on one market."""
    market_ticker:   str
    yes_bid_cents:   Optional[int]  # None = no yes quote
    no_bid_cents:    Optional[int]  # None = no no quote
    size_contracts:  int            # default size for both sides
    # #97 (2026-04-28) optional per-side overrides for inventory skew. When
    # we're long YES, set no_size_override > yes_size_override to lean into
    # absorbing offsetting fills. None = use size_contracts.
    yes_size_override: Optional[int] = None
    no_size_override:  Optional[int] = None

    def yes_size(self) -> int:
        return self.yes_size_override if self.yes_size_override is not None else self.size_contracts

    def no_size(self) -> int:
        return self.no_size_override if self.no_size_override is not None else self.size_contracts


@dataclass
class RestingOrder:
    """A currently-open order we've placed."""
    order_id:       str
    market_ticker:  str
    side:           str            # "yes" | "no"
    price_cents:    int
    size_contracts: int
    placed_at:      float          # unix ts
    paper:          bool = True
    # 2026-05-02 PREDATOR C2: cancel was requested but API call failed.
    # Flag prevents placing a duplicate same-side order until next reconcile
    # cycle clears the in-memory state via periodic_resync.
    pending_cancel: bool = False


@dataclass
class InventoryState:
    """Per-market inventory from fills."""
    market_ticker:     str
    net_yes_contracts: int = 0     # signed; positive = long yes
    gross_usd:         float = 0.0
    total_filled_vol:  int = 0     # absolute contracts traded
    last_updated:      float = 0.0


class QuoteManager:
    """Stateful manager. One instance per engine run.

    Paper mode: logs intent to quotes table, never calls /portfolio/orders.
    Live mode (when PAPER_MODE=False): places/cancels real orders.
    """

    def __init__(self, *, paper: bool = True, db_path: Optional[str] = None):
        self.paper = paper if paper is not None else settings.PAPER_MODE
        self.db_path = db_path or settings.DB_PATH
        self.client = KalshiClient() if not self.paper else None
        self.resting: dict[str, list[RestingOrder]] = {}   # ticker -> orders
        self.inventory: dict[str, InventoryState] = {}     # ticker -> state
        self._last_balance_check = 0.0
        self._cached_balance = 0.0
        # 2026-04-25 COLD-BOOT RECONCILIATION: rehydrate self.resting from
        # Kalshi's actual order book on init. Without this, a service
        # restart leaves us blind to live orders → next reconcile() places
        # duplicate orders and we get filled twice. Live mode only.
        self._cold_boot_reconcile()

    def _cold_boot_reconcile(self) -> None:
        """Query Kalshi for currently-resting orders and hydrate self.resting.

        Idempotent + fail-safe: if API errors, we log and continue with
        empty self.resting (same as previous behavior — no regression).
        """
        if self.paper or self.client is None:
            return
        try:
            cursor = None
            rehydrated = 0
            while True:
                params = {"status": "resting", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                resp = self.client.get("/portfolio/orders", params=params)
                for raw in resp.get("orders", []):
                    if raw.get("status") != "resting":
                        continue
                    side = raw.get("side", "").lower()
                    if side not in ("yes", "no"):
                        continue
                    # Kalshi v2 uses decimal strings: "remaining_count_fp"
                    # and prices as "yes_price_dollars" / "no_price_dollars".
                    price_field = "yes_price_dollars" if side == "yes" else "no_price_dollars"
                    price_str = raw.get(price_field)
                    if price_str is None:
                        continue
                    price = int(round(float(price_str) * 100))
                    ticker = raw.get("ticker", "")
                    order_id = raw.get("order_id", "")
                    try:
                        qty = int(float(raw.get("remaining_count_fp")
                                        or raw.get("initial_count_fp", 0)))
                    except (TypeError, ValueError):
                        qty = 0
                    if not ticker or not order_id or qty <= 0:
                        continue
                    self.resting.setdefault(ticker, []).append(
                        RestingOrder(
                            order_id=order_id,
                            market_ticker=ticker,
                            side=side,
                            price_cents=int(price),
                            size_contracts=qty,
                            placed_at=time.time(),  # we don't know real placement time
                            paper=False,
                        )
                    )
                    rehydrated += 1
                cursor = resp.get("cursor")
                if not cursor:
                    break
            _log.warning(f"cold-boot: rehydrated {rehydrated} resting orders "
                         f"across {len(self.resting)} tickers from Kalshi")
        except Exception as e:
            _log.warning(f"cold-boot reconcile FAILED ({e}) — starting with empty resting state")

    def periodic_resync(self) -> dict:
        """Reconcile self.resting against live Kalshi orders. Cures drift
        from filled/cancelled orders that didn't get purged from memory.

        Removes phantom entries (in memory but not on Kalshi) and ADDS any
        live orders missing from memory (rare but happens after WS hiccup).

        Run from heartbeat every ~5 min. Cheap (one /portfolio/orders call).
        """
        if self.paper or self.client is None:
            return {"skipped": "paper_mode"}
        try:
            live_ids = set()
            cursor = None
            while True:
                params = {"status": "resting", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                resp = self.client.get("/portfolio/orders", params=params)
                for raw in resp.get("orders", []):
                    if raw.get("status") == "resting" and raw.get("order_id"):
                        live_ids.add(raw["order_id"])
                cursor = resp.get("cursor")
                if not cursor:
                    break
            # Purge phantoms
            phantoms = 0
            for ticker, orders in list(self.resting.items()):
                kept = [o for o in orders if o.order_id in live_ids]
                phantoms += len(orders) - len(kept)
                if kept:
                    self.resting[ticker] = kept
                else:
                    self.resting.pop(ticker, None)
            if phantoms > 0:
                _log.info(f"periodic_resync: purged {phantoms} phantom entries "
                          f"from self.resting (cured drift)")
            return {"phantoms_purged": phantoms, "live_count": len(live_ids)}
        except Exception as e:
            _log.warning(f"periodic_resync failed: {e}")
            return {"error": str(e)}

    # ── Inventory tracking ────────────────────────────────────────────
    def _refresh_inventory(self, market_ticker: str) -> None:
        """Populate self.inventory[market_ticker] from fill_ledger.

        2026-04-22 (Spread Master audit): self.inventory was declared but
        never populated → MAX_NET_INVENTORY_USD cap was theatrical. This
        method computes net position from unsettled fills, cached
        per-ticker to avoid DB spam (similar to balance cache pattern).

        Net YES = (yes fills count) − (no fills count), since buying NO
        is mathematically shorting YES (YES + NO = $1.00).

        Excludes tickers already in settlement_log (settled = realized PnL,
        not exposure).

        2026-05-01 PREDATOR: cache TTL 30s → 5s. The 30s window was hiding
        offsetting fills from the #97 inventory skew, killing recirculation.
        At 5s we react within ~6 book-update cycles instead of ~36, turning
        passive position decay into active inventory cycling.
        """
        existing = self.inventory.get(market_ticker)
        now = time.time()
        if existing and (now - existing.last_updated) < 5:
            return  # cached
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                row = conn.execute(
                    """SELECT
                           COALESCE(SUM(CASE WHEN side='yes' THEN count ELSE -count END), 0),
                           COALESCE(SUM(count), 0),
                           COALESCE(SUM(CASE WHEN side='yes' THEN count * yes_price_cents
                                             ELSE count * no_price_cents END) / 100.0, 0)
                       FROM fill_ledger
                       WHERE ticker = ?
                         AND ticker NOT IN (SELECT ticker FROM settlement_log)""",
                    (market_ticker,),
                ).fetchone()
            finally:
                conn.close()
            self.inventory[market_ticker] = InventoryState(
                market_ticker=market_ticker,
                net_yes_contracts=int(row[0] or 0),
                total_filled_vol=int(row[1] or 0),
                gross_usd=float(row[2] or 0),
                last_updated=now,
            )
        except Exception as e:
            _log.warning(f"inventory refresh failed for {market_ticker}: {e}")
            # Keep existing (may be stale) rather than wiping. Better stale than empty.

    # ── Balance + aggregate risk ──────────────────────────────────────
    def _get_balance(self) -> float:
        if self.paper:
            return 10_000.0  # pretend $10k for paper risk math
        # Refresh balance every 30 seconds to avoid spamming the API
        now = time.time()
        if now - self._last_balance_check > 30:
            try:
                self._cached_balance = self.client.get_balance()
                self._last_balance_check = now
            except Exception as e:
                _log.warning(f"balance fetch failed: {e}")
        return self._cached_balance

    def _series_gross(self, series_prefix: str) -> float:
        """Sum gross USD exposure across all resting orders matching a series prefix.
        2026-04-22 (Skeptic audit): used by per-series cap to prevent single-underlying
        flash crash blowup (e.g., Brent moving through 5+ strikes simultaneously).
        """
        prefix_with_dash = series_prefix + "-"
        return sum(
            o.price_cents * o.size_contracts / 100.0
            for ticker, orders in self.resting.items()
            if ticker.startswith(prefix_with_dash)
            for o in orders
        )

    def _total_gross_exposure(self) -> float:
        return sum(
            o.price_cents * o.size_contracts / 100.0
            for orders in self.resting.values()
            for o in orders
        )

    # ── Daily loss circuit breaker ──────────────────────────────────
    def _daily_realized_pnl(self) -> float:
        """Sum of realized PnL from fills in the last 24h. Live mode only.

        2026-04-22 (Skeptic audit): FAIL-SAFE on DB error. Previously returned
        0.0 on exception → DB lock during high vol = circuit breaker silently
        disabled. Now returns -inf so the comparison `daily_pnl < -MAX_DAILY_LOSS`
        is always True and quoting halts. The error is logged at WARNING so we
        see it instead of swallowing.
        """
        if self.paper:
            return 0.0
        try:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                row = conn.execute(
                    """SELECT COALESCE(SUM(
                            (fill_price_cents - price_cents) * fill_size / 100.0), 0)
                       FROM quotes
                       WHERE filled_at >= ? AND fill_size IS NOT NULL""",
                    (cutoff,),
                ).fetchone()
                return float(row[0] or 0.0)
            finally:
                conn.close()
        except Exception as e:
            _log.warning(f"circuit_breaker DB read failed: {e} — failing safe (halt)")
            return float("-inf")

    # ── Pre-trade safety ──────────────────────────────────────────────
    def _passes_safety(self, target: QuoteTarget) -> tuple[bool, str]:
        """Run all gates. Returns (passes, reason)."""
        # Market blacklist — auto-blocks after repeated losses
        from execution.market_blacklist import is_blocked
        blocked, reason = is_blocked(target.market_ticker, self.db_path)
        if blocked:
            return False, f"BLACKLIST: {reason}"
        # #103 Pre-trade EV check — refuse series with negative 7d EV
        from engine.series_ev import check_series_ev
        series_prefix = target.market_ticker.split("-", 1)[0]
        ev_ok, ev_reason = check_series_ev(series_prefix, self.db_path)
        if not ev_ok:
            return False, ev_reason
        # Circuit breaker: halt all quoting if daily loss exceeds cap (live only)
        if not self.paper:
            daily_pnl = self._daily_realized_pnl()
            if daily_pnl < -settings.MAX_DAILY_LOSS_USD:
                return False, (f"CIRCUIT_BREAKER: daily_pnl ${daily_pnl:.2f} "
                               f"< -${settings.MAX_DAILY_LOSS_USD:.0f} (halting)")
        # AUDIT FIX (2026-04-28): when #97 inventory skew sets per-side
        # overrides, the asymmetric placement size can be up to 1.5×
        # size_contracts. All downstream caps (per-mkt gross, total
        # gross, series gross, bankroll share) must use the LARGER
        # actual placement size to avoid undersized accounting.
        effective_size = max(
            target.size_contracts,
            target.yes_size_override or 0,
            target.no_size_override or 0,
        )
        if effective_size < settings.MIN_QUOTE_SIZE_CONTRACTS:
            return False, f"size<{settings.MIN_QUOTE_SIZE_CONTRACTS}"

        # Spread check
        if target.yes_bid_cents is not None and target.no_bid_cents is not None:
            implied_yes_ask = 100 - target.no_bid_cents
            spread = implied_yes_ask - target.yes_bid_cents
            if spread > settings.MAX_SPREAD_CENTS or spread < 0:
                return False, f"spread={spread}c"

        # 2026-05-01 TWO-SIDED LIQUIDITY GATE — fixes adverse-selection trap
        # where we enter markets with bid on only ONE side. 40 of 96 stranded
        # positions today were stuck because the side we'd need to exit had
        # no buyers. Min 5c bid required on BOTH sides to confirm two-sided
        # demand. If we can't exit, we shouldn't enter.
        if target.yes_bid_cents is not None and target.no_bid_cents is not None:
            if (target.yes_bid_cents < settings.MIN_TWO_SIDED_BID_CENTS or
                target.no_bid_cents < settings.MIN_TWO_SIDED_BID_CENTS):
                return False, (f"one_sided_book: yes_bid={target.yes_bid_cents}c "
                               f"no_bid={target.no_bid_cents}c "
                               f"(need ≥{settings.MIN_TWO_SIDED_BID_CENTS} both)")

        # Per-market gross — DOWNSIZE target to fit cap instead of rejecting.
        # 2026-04-22: was rejecting outright, leaving rebate on the table for
        # borderline markets (gross just over cap). Now size-reduces.
        # 2026-04-28 (#126): per-series cap override allows grain weeklies
        # to deploy 60%+ of pool instead of 27%. Default for everything else.
        per_mkt = sum(
            o.price_cents * o.size_contracts / 100.0
            for o in self.resting.get(target.market_ticker, [])
        )
        series_for_cap = target.market_ticker.split("-", 1)[0]
        per_market_cap = settings.MAX_GROSS_PER_MARKET_BY_SERIES.get(
            series_for_cap, settings.MAX_GROSS_PER_MARKET_USD,
        )
        remaining_cap = per_market_cap - per_mkt
        if remaining_cap <= 0:
            return False, f"per_mkt_gross ${per_mkt:.2f} > cap ${per_market_cap}"

        # Cost per contract for both sides combined
        sides = 0
        price_sum = 0
        if target.yes_bid_cents is not None:
            price_sum += target.yes_bid_cents
            sides += 1
        if target.no_bid_cents is not None:
            price_sum += target.no_bid_cents
            sides += 1
        if sides == 0:
            return True, "ok"   # nothing to place anyway
        cost_per_contract = price_sum / 100.0   # $ per contract across both sides

        max_contracts_by_cap = int(remaining_cap / cost_per_contract) if cost_per_contract > 0 else effective_size
        if effective_size > max_contracts_by_cap:
            if max_contracts_by_cap < settings.MIN_QUOTE_SIZE_CONTRACTS:
                return False, (f"cap_too_tight remaining=${remaining_cap:.2f} "
                               f"allows {max_contracts_by_cap} < MIN {settings.MIN_QUOTE_SIZE_CONTRACTS}")
            # Downsize in place — scale BOTH base size AND any overrides
            # proportionally so per-side asymmetry survives the downsize.
            scale = max_contracts_by_cap / effective_size
            target.size_contracts = max(
                settings.MIN_QUOTE_SIZE_CONTRACTS,
                int(target.size_contracts * scale),
            )
            if target.yes_size_override is not None:
                target.yes_size_override = max(
                    settings.MIN_QUOTE_SIZE_CONTRACTS,
                    int(target.yes_size_override * scale),
                )
            if target.no_size_override is not None:
                target.no_size_override = max(
                    settings.MIN_QUOTE_SIZE_CONTRACTS,
                    int(target.no_size_override * scale),
                )
            effective_size = max(
                target.size_contracts,
                target.yes_size_override or 0,
                target.no_size_override or 0,
            )

        # Per-market net inventory — refresh from fill_ledger first (Spread Master fix)
        self._refresh_inventory(target.market_ticker)
        inv = self.inventory.get(target.market_ticker)
        if inv:
            # Net inventory value at current mid price ≈ net × 50c (pessimistic mid)
            net_usd = abs(inv.net_yes_contracts) * 0.50
            if net_usd > settings.MAX_NET_INVENTORY_USD:
                return False, f"net_inv ${net_usd:.2f} > cap ${settings.MAX_NET_INVENTORY_USD}"

        # Total gross — use the LARGER of base + overrides (audit fix)
        total_gross = self._total_gross_exposure()
        new_incr = (price_sum / 100.0) * effective_size
        if total_gross + new_incr > settings.MAX_TOTAL_GROSS_USD:
            return False, f"total_gross ${total_gross+new_incr:.2f} > cap ${settings.MAX_TOTAL_GROSS_USD}"

        # Per-series gross (Skeptic audit) — cap exposure per underlying.
        # Series prefix = part before first "-" (e.g., "KXBRENTD" from
        # "KXBRENTD-26APR2317-T103"). Prevents Brent flash-crash style
        # multi-strike simultaneous blowup.
        series_prefix = target.market_ticker.split("-", 1)[0]
        series_gross = self._series_gross(series_prefix)
        if series_gross + new_incr > settings.MAX_GROSS_PER_SERIES_USD:
            return False, (f"series_gross[{series_prefix}] "
                           f"${series_gross+new_incr:.2f} > cap "
                           f"${settings.MAX_GROSS_PER_SERIES_USD:.2f}")

        # Bankroll share
        balance = self._get_balance()
        cap_usd = balance * settings.MAX_BANKROLL_SHARE_PCT
        if total_gross + new_incr > cap_usd:
            return False, f"bankroll_share ${total_gross+new_incr:.2f} > {settings.MAX_BANKROLL_SHARE_PCT*100:.0f}% of ${balance:.2f}"

        return True, "ok"

    # ── Order placement (paper or live) ───────────────────────────────
    def _place_order(
        self,
        market_ticker: str,
        side: str,
        price_cents: int,
        size_contracts: int,
    ) -> Optional[RestingOrder]:
        """Place a single resting limit order."""
        coid = f"LIP-{uuid.uuid4().hex[:16]}"
        # 2026-04-30 audit fix: skip edge-priced orders (Kalshi rejects 0 and 100
        # cent prices as "invalid price"). Avoids ERROR-log spam + rate-limit hits.
        if price_cents <= 0 or price_cents >= 100:
            _log.debug(f"[SKIP] {market_ticker} {side}@{price_cents}c — edge price (Kalshi rejects)")
            return None
        if self.paper:
            order_id = "PAPER-" + coid
            _log.info(f"[PAPER] PLACE {market_ticker} {side}@{price_cents}c size={size_contracts}")
        else:
            body = {
                "ticker": market_ticker,
                "side":   side,
                "action": "buy",
                "type":   "limit",
                "count":  size_contracts,
                "post_only": True,  # NEXUS-OMNI V4 D1: never accidentally cross — pure maker
                "client_order_id": coid,
            }
            # yes_price for yes side, no_price for no side
            if side == "yes":
                body["yes_price"] = price_cents
            else:
                body["no_price"] = price_cents
            try:
                resp = self.client.post("/portfolio/orders", body)
                order_id = resp.get("order", {}).get("order_id", "")
                _log.info(f"[LIVE] PLACED {market_ticker} {side}@{price_cents}c size={size_contracts} order_id={order_id}")
            except Exception as e:
                _log.error(f"order placement failed for {market_ticker} {side}@{price_cents}: {e}")
                self._log_quote_row(market_ticker, side, price_cents, size_contracts,
                                      coid, "rejected", notes=str(e))
                return None

        rest = RestingOrder(
            order_id=order_id, market_ticker=market_ticker,
            side=side, price_cents=price_cents, size_contracts=size_contracts,
            placed_at=time.time(), paper=self.paper,
        )
        # #127 (2026-04-28) UPSERT semantics: drop any existing entry for
        # this (ticker, side) before appending. Prevents accumulation when
        # _cancel_order silently failed (network error returns False but
        # leaves entry in self.resting). Closes the race that triggered
        # premature per_mkt_gross safety gate hits in paper mode.
        existing_lst = self.resting.setdefault(market_ticker, [])
        stale_same_side = [o for o in existing_lst if o.side == side]
        for o in stale_same_side:
            existing_lst.remove(o)
            # Mark stale entry's DB row cancelled so it doesn't double-count
            # in any future reporting query.
            self._update_quote_status(o.order_id, "cancelled",
                                      notes="upsert_replaced_by_127")
        existing_lst.append(rest)
        self._log_quote_row(market_ticker, side, price_cents, size_contracts,
                              order_id, "resting", notes=f"coid={coid}")
        return rest

    def _cancel_order(self, order: RestingOrder) -> bool:
        """Cancel a resting order.

        2026-04-22 FIX: On 404 (order already gone from Kalshi — filled, expired,
        or previously cancelled), treat as success and STILL purge from
        self.resting. Prior behavior returned False without removing, causing
        phantom orders to accumulate → safety gate computed inflated per_mkt_gross
        → top-reward markets (VOTEHUBTRUMPUPDOWN, MAMDANIEO, EOWEEK, etc.)
        blocked indefinitely.
        """
        if self.paper:
            _log.info(f"[PAPER] CANCEL {order.market_ticker} {order.side}@{order.price_cents}c")
        else:
            try:
                self.client.delete(f"/portfolio/orders/{order.order_id}")
                _log.info(f"[LIVE] CANCELLED {order.market_ticker} {order.side}@{order.price_cents}c order_id={order.order_id}")
            except Exception as e:
                if "404" in str(e) or "Not Found" in str(e):
                    # Order already gone — purge from memory, don't block.
                    _log.info(f"[LIVE] 404-PURGE phantom {order.market_ticker} order_id={order.order_id}")
                else:
                    _log.warning(f"cancel failed for {order.order_id}: {e}")
                    return False

        # Remove from in-memory resting (runs on all paths except non-404 error)
        lst = self.resting.get(order.market_ticker, [])
        try:
            lst.remove(order)
        except ValueError:
            pass
        # UPDATE existing row (don't INSERT a new cancelled one) — otherwise the
        # quotes table grows unbounded and confuses per-market counting.
        self._update_quote_status(order.order_id, "cancelled", notes="manager_cancel")
        return True

    # ── Reconciliation: align resting vs target ───────────────────────
    def _sanity_resting(self, market_ticker: str) -> None:
        """Defensive: keep at most 1 yes + 1 no per market in self.resting.

        Under normal operation reconcile maintains this, but WS re-subscribe
        or exceptions mid-reconcile can leave phantom entries. If any market
        has any duplicate-side entries, keep the most-recent per side and
        drop the rest.

        AUDIT FIX 2026-04-28 (#127 follow-up): was `len(lst) <= 2` — that
        allowed [yes, yes] state to slip past sanity check. Now cleans
        any side that has >1 entry. Drains leaks from BEFORE #127 UPSERT
        was deployed.
        """
        lst = self.resting.get(market_ticker, [])
        if not lst:
            return
        yes_orders = sorted([o for o in lst if o.side == "yes"],
                            key=lambda o: o.placed_at, reverse=True)
        no_orders  = sorted([o for o in lst if o.side == "no"],
                            key=lambda o: o.placed_at, reverse=True)
        if len(yes_orders) <= 1 and len(no_orders) <= 1:
            return  # already clean
        kept = (yes_orders[:1] + no_orders[:1])
        stale = [o for o in lst if o not in kept]
        for o in stale:
            self._update_quote_status(o.order_id, "cancelled", notes="sanity_purge")
        self.resting[market_ticker] = kept
        _log.warning(f"sanity_resting cleaned {market_ticker}: {len(lst)} → {len(kept)}")

    def reset_for_market(self, market_ticker: str) -> None:
        """Drop all in-memory resting entries for a market (used on WS re-subscribe)."""
        lst = self.resting.pop(market_ticker, [])
        for o in lst:
            self._update_quote_status(o.order_id, "cancelled", notes="ws_resubscribe_reset")
        if lst:
            _log.info(f"reset_for_market {market_ticker}: dropped {len(lst)} in-memory orders")

    def reconcile(self, target: QuoteTarget) -> dict:
        """Bring resting orders in line with target for one market.

        Returns dict summarizing actions taken.
        """
        self._sanity_resting(target.market_ticker)
        ok, reason = self._passes_safety(target)
        if not ok:
            # Throttle per-market: log once per 60s per (ticker, reason) combo.
            # Safety gates firing every book update floods the log.
            import time as _time
            key = (target.market_ticker, reason.split(":")[0] if ":" in reason else reason)
            last = getattr(self, "_last_gate_log", {}).get(key, 0)
            if _time.time() - last > 60:
                if not hasattr(self, "_last_gate_log"):
                    self._last_gate_log = {}
                self._last_gate_log[key] = _time.time()
                _log.info(f"safety gate failed for {target.market_ticker}: {reason}")
            return {"action": "skip", "reason": reason}

        current = self.resting.get(target.market_ticker, [])
        current_yes = [o for o in current if o.side == "yes"]
        current_no  = [o for o in current if o.side == "no"]

        actions = {"cancelled": 0, "placed": 0, "kept": 0, "reason": reason}

        # Yes side (#97: respects yes_size_override for inventory skew)
        yes_size = target.yes_size()
        if target.yes_bid_cents is None:
            for o in current_yes:
                if self._cancel_order(o):
                    actions["cancelled"] += 1
        else:
            need_replace = False
            if len(current_yes) != 1:
                need_replace = True
            elif current_yes[0].price_cents != target.yes_bid_cents or current_yes[0].size_contracts != yes_size:
                need_replace = True
            if need_replace:
                # 2026-05-02 PREDATOR C2: place ONLY if all cancels succeeded.
                # Was: cancel returns False → we still place → two same-side
                # orders co-exist → safety caps drift, double exposure. Now:
                # any cancel failure marks the order pending_cancel and skips
                # placement until next reconcile cycle (periodic_resync clears).
                all_cancelled = True
                for o in current_yes:
                    if self._cancel_order(o):
                        actions["cancelled"] += 1
                    else:
                        o.pending_cancel = True
                        all_cancelled = False
                        _log.warning(f"C2 cancel-failed yes {target.market_ticker} "
                                     f"oid={o.order_id[:12]} — marking pending_cancel, "
                                     f"skipping placement to avoid duplicate")
                if all_cancelled:
                    r = self._place_order(target.market_ticker, "yes",
                                           target.yes_bid_cents, yes_size)
                    if r:
                        actions["placed"] += 1
                else:
                    actions["pending_cancel_yes"] = sum(
                        1 for o in current_yes if o.pending_cancel
                    )
            else:
                actions["kept"] += 1

        # No side (#97: respects no_size_override for inventory skew)
        no_size = target.no_size()
        if target.no_bid_cents is None:
            for o in current_no:
                if self._cancel_order(o):
                    actions["cancelled"] += 1
        else:
            need_replace = False
            if len(current_no) != 1:
                need_replace = True
            elif current_no[0].price_cents != target.no_bid_cents or current_no[0].size_contracts != no_size:
                need_replace = True
            if need_replace:
                # 2026-05-02 PREDATOR C2: same race fix as yes side above.
                all_cancelled = True
                for o in current_no:
                    if self._cancel_order(o):
                        actions["cancelled"] += 1
                    else:
                        o.pending_cancel = True
                        all_cancelled = False
                        _log.warning(f"C2 cancel-failed no {target.market_ticker} "
                                     f"oid={o.order_id[:12]} — marking pending_cancel, "
                                     f"skipping placement to avoid duplicate")
                if all_cancelled:
                    r = self._place_order(target.market_ticker, "no",
                                           target.no_bid_cents, no_size)
                    if r:
                        actions["placed"] += 1
                else:
                    actions["pending_cancel_no"] = sum(
                        1 for o in current_no if o.pending_cancel
                    )
            else:
                actions["kept"] += 1

        return actions

    def cancel_all(self, market_ticker: Optional[str] = None):
        """Cancel every resting order, optionally scoped to one market."""
        if market_ticker:
            orders = list(self.resting.get(market_ticker, []))
        else:
            orders = [o for lst in self.resting.values() for o in lst]
        for o in orders:
            self._cancel_order(o)

    # ── Logging ───────────────────────────────────────────────────────
    def _log_quote_row(
        self, market_ticker: str, side: str, price_cents: int,
        size_contracts: int, order_id: str, status: str,
        *, notes: str = "",
    ):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO quotes
                   (market_ticker, side, price_cents, size_contracts,
                    order_id, status, paper, placed_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (market_ticker, side, price_cents, size_contracts,
                 order_id, status, 1 if self.paper else 0,
                 datetime.now(timezone.utc).isoformat(), notes),
            )
            conn.commit()
        finally:
            conn.close()

    def _update_quote_status(
        self, order_id: str, status: str, *, notes: str = "",
    ):
        """UPDATE (not INSERT) the existing quote row to a terminal status.

        Prevents the quotes table from growing unbounded with duplicate
        resting/cancelled rows for the same order_id.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            ts = datetime.now(timezone.utc).isoformat()
            col = {"cancelled": "cancelled_at", "filled": "filled_at"}.get(status)
            if col:
                conn.execute(
                    f"""UPDATE quotes SET status = ?, {col} = ?, notes = ?
                        WHERE order_id = ? AND status = 'resting'""",
                    (status, ts, notes, order_id),
                )
            else:
                conn.execute(
                    """UPDATE quotes SET status = ?, notes = ?
                       WHERE order_id = ? AND status = 'resting'""",
                    (status, notes, order_id),
                )
            conn.commit()
        finally:
            conn.close()

    def summary(self) -> dict:
        return {
            "paper": self.paper,
            "n_markets_with_orders": len(self.resting),
            "n_total_orders": sum(len(v) for v in self.resting.values()),
            "total_gross_usd": self._total_gross_exposure(),
            "balance_usd": self._cached_balance,
        }


# ── Basic tests ────────────────────────────────────────────────────────

def _self_test():
    """Basic quote-manager sanity checks (paper mode)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from init_db import init_db
    init_db()

    qm = QuoteManager(paper=True)

    # Simple target
    target = QuoteTarget(
        market_ticker="KXTEST-DEMO",
        yes_bid_cents=45,
        no_bid_cents=50,
        size_contracts=25,
    )
    # First reconcile: should place both yes + no
    r = qm.reconcile(target)
    assert r["placed"] == 2, f"expected 2 placed, got {r}"

    # Second reconcile with same target: should keep both
    r = qm.reconcile(target)
    assert r["kept"] == 2, f"expected 2 kept, got {r}"

    # Change yes price: should cancel + replace yes, keep no
    target.yes_bid_cents = 46
    r = qm.reconcile(target)
    assert r["cancelled"] == 1 and r["placed"] == 1 and r["kept"] == 1, f"got {r}"

    # Safety: oversize violates MIN_QUOTE_SIZE? Actually ours is 25 ≥ 10, ok.
    # Test spread rejection:
    target_wide = QuoteTarget(market_ticker="KXTEST-WIDE",
                              yes_bid_cents=20, no_bid_cents=30,  # implied ask 70c, spread 50c
                              size_contracts=25)
    r = qm.reconcile(target_wide)
    assert r["action"] == "skip", f"wide spread should skip: got {r}"

    # Cancel all
    qm.cancel_all()
    assert all(len(v) == 0 for v in qm.resting.values())

    print("quote_manager self-test PASSED")
    print("summary:", qm.summary())


if __name__ == "__main__":
    _self_test()
