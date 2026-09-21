"""Kalshi implementation of the Venue interface.

Wraps the existing `execution.kalshi_auth.KalshiClient` + `execution.kalshi_ws.KalshiWS`
code so we don't have to rewrite what already works. New multi-venue code
(quote_manager-v2, profit-attribution-v2, toxicity-filter-v2) consumes this
Venue interface; legacy code can still use the underlying clients directly
until migrated.
"""
from __future__ import annotations

import uuid
import logging
from typing import Awaitable, Callable, Optional

from venue.base import (
    Venue, Position, Fill, OrderResult, OrderbookSnapshot, BookLevel,
    MarketMetadata, register_venue,
)
from execution.kalshi_auth import KalshiClient
from execution.order_request import (
    MakerSafetyError, assert_maker_safe, build_limit_order,
)

_log = logging.getLogger(__name__)


@register_venue
class KalshiVenue(Venue):
    name = "kalshi"

    def __init__(self):
        self._client = KalshiClient()
        self._ws = None

    # ── Account state ──

    def get_balance(self) -> float:
        return self._client.get_balance()

    def get_positions(self) -> list[Position]:
        # Paginate positions — follows fix from audit #7
        all_positions: list[dict] = []
        cursor = None
        for _ in range(10):  # max 10 pages safety
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = self._client.get("/portfolio/positions", params=params)
            all_positions.extend(r.get("market_positions", []))
            cursor = r.get("cursor")
            if not cursor:
                break

        out: list[Position] = []
        for p in all_positions:
            net = int(float(p.get("position_fp", "0") or 0))
            if net == 0:
                continue
            out.append(Position(
                ticker=p.get("ticker", ""),
                net_contracts=net,
                avg_entry_cents=None,     # Kalshi doesn't expose this on positions endpoint
                market_exposure_usd=float(p.get("market_exposure_dollars", "0") or 0),
                realized_pnl_usd=float(p.get("realized_pnl_dollars", "0") or 0),
            ))
        return out

    def get_fills(self, since_iso: Optional[str] = None,
                  limit: int = 200) -> list[Fill]:
        out: list[Fill] = []
        cursor = None
        for _ in range(10):  # max 10 pages
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            try:
                r = self._client.get("/portfolio/fills", params=params)
            except Exception as e:
                _log.warning(f"fills fetch failed at cursor={cursor}: {e}")
                break

            for f in r.get("fills", []):
                created = f.get("created_time", "")
                if since_iso and created < since_iso:
                    return out  # past the cutoff — stop
                side = f.get("side", "?")
                yp = int(round(float(f.get("yes_price_dollars", "0") or 0) * 100))
                np_ = int(round(float(f.get("no_price_dollars", "0") or 0) * 100))
                price = yp if side == "yes" else np_
                out.append(Fill(
                    trade_id=f.get("trade_id", ""),
                    order_id=f.get("order_id", ""),
                    ticker=f.get("ticker", f.get("market_ticker", "")),
                    side=side,
                    count=int(float(f.get("count_fp", "0") or 0)),
                    price_cents=price,
                    is_maker=not bool(f.get("is_taker")),
                    created_at=created,
                ))
            cursor = r.get("cursor")
            if not cursor:
                break
        return out

    # ── Orders ──

    def place_order(self, ticker: str, side: str, price_cents: int,
                    size_contracts: int, post_only: bool = True,
                    best_opposing_bid_cents: Optional[int] = None) -> OrderResult:
        """Place a maker-only limit order via the shared request builder.

        2026-09-21: this method used to substitute `no_self_trade` for
        `post_only`, asserting the latter did not exist, while
        execution/quote_manager.py sent `post_only`. The two adapters
        disagreed about what maker protection means, and `no_self_trade` is
        not maker protection — it only blocks trading against your OWN
        resting order, not crossing a stranger's offer.

        Both paths now build the body in execution.order_request, which
        proves the order is non-crossing locally before it is sent. Maker
        safety no longer depends on an API flag this environment cannot
        verify (docs.kalshi.com is egress-blocked).

        `best_opposing_bid_cents` is the other side's best bid. Without it a
        maker order cannot be proven passive and is refused.
        """
        if side not in ("yes", "no"):
            return OrderResult(success=False, error=f"invalid side: {side}")
        try:
            body = build_limit_order(
                ticker=ticker, side=side, price_cents=int(price_cents),
                size_contracts=int(size_contracts),
                client_order_id=f"innait-{uuid.uuid4().hex[:12]}",
                best_opposing_bid_cents=best_opposing_bid_cents,
                enforce_non_crossing=post_only,
                time_in_force="GTC",
            )
            if post_only:
                assert_maker_safe(body)
        except MakerSafetyError as e:
            return OrderResult(success=False, error=f"maker safety: {e}")
        try:
            resp = self._client.post("/portfolio/orders", body)
            order_id = (resp.get("order") or {}).get("order_id")
            if order_id:
                return OrderResult(success=True, order_id=order_id, raw=resp)
            return OrderResult(success=False, error="no order_id in response", raw=resp)
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.delete(f"/portfolio/orders/{order_id}")
            return True
        except Exception as e:
            # 404 = already cancelled/filled = effectively success for our purposes
            if "404" in str(e):
                return True
            _log.warning(f"cancel failed {order_id}: {e}")
            return False

    def cancel_all_for_ticker(self, ticker: str) -> int:
        try:
            orders = self._client.get("/portfolio/orders",
                                       params={"status": "resting", "ticker": ticker, "limit": 200})
            count = 0
            for o in orders.get("orders", []):
                oid = o.get("order_id")
                if oid and self.cancel_order(oid):
                    count += 1
            return count
        except Exception as e:
            _log.warning(f"cancel_all_for_ticker({ticker}) failed: {e}")
            return 0

    # ── Market data ──

    def get_market_metadata(self, ticker: str) -> MarketMetadata:
        r = self._client.get_unauth(f"/markets/{ticker}")
        m = r.get("market", {}) or {}
        return MarketMetadata(
            ticker=ticker,
            tick_size_cents=int(m.get("tick_size", 1)),
            min_size=1,  # Kalshi allows 1-contract orders
            close_time=m.get("close_time"),
            settlement_rule=m.get("rules_primary"),
            extra={
                "title":           m.get("title"),
                "status":          m.get("status"),
                "expected_expiration_time": m.get("expected_expiration_time"),
                "floor_strike":    m.get("floor_strike"),
            },
        )

    async def stream_orderbook(self, tickers: list[str],
                                on_update: Callable[[OrderbookSnapshot], Awaitable[None]]
                               ) -> None:
        """Wraps existing KalshiWS. Converts BookState → OrderbookSnapshot."""
        from execution.kalshi_ws import KalshiWS, BookState as KalshiBookState
        from datetime import datetime, timezone

        ws = KalshiWS()
        await ws.connect()

        async def _adapter(book: KalshiBookState):
            snap = OrderbookSnapshot(
                ticker=book.market_ticker,
                yes_bids=[BookLevel(price_cents=l.price_cents, size=l.size)
                          for l in book.yes_bids],
                no_bids=[BookLevel(price_cents=l.price_cents, size=l.size)
                         for l in book.no_bids],
                captured_at=datetime.now(timezone.utc).isoformat(),
            )
            await on_update(snap)

        ws.on_update(_adapter)
        await ws.subscribe_orderbook(tickers)
        self._ws = ws
        await ws.run()
