"""Kalshi adapter on the V2 event-order API.

Paths, fetched 2026-10-01 from docs.kalshi.com (OpenAPI 3.32.0):

    POST   /portfolio/events/orders                         create
    DELETE /portfolio/events/orders/{order_id}              cancel
    POST   /portfolio/events/orders/{order_id}/decrease     size down
    POST   /portfolio/events/orders/{order_id}/amend        price or size
    GET    /portfolio/orders/{order_id}/queue_position      queue ahead
    POST   /portfolio/order_groups/create                   rolling 15s cap

The legacy ``/portfolio/orders`` mutation routes are deprecated no earlier
than 6 May 2026 (Create Order V2 description) and, per the changelog, were
scheduled to start rejecting between 18 and 25 June 2026. This adapter does
not call them.

Book side is the YES leg only. A NO buy at q cents is an ask to sell YES at
``1 - q/100``.
"""
from __future__ import annotations

import uuid
from typing import Optional
from urllib.parse import urlencode

from execution.order_request import (
    LiveExecutionBlocked, MakerSafetyError, build_limit_order,
    require_live_execution_allowed, to_event_order_v2,
)
from mm.types import Side, VenueName, VenueOrderView
from mm.venues.base import RateBudget, Transport, dollars_to_cents, parse_fp

CREATE = "/portfolio/events/orders"
CANCEL_TOKENS = 2.0
CREATE_TOKENS = 10.0


class KalshiAdapter:
    name = VenueName.KALSHI

    def __init__(self, transport: Optional[Transport] = None, *, paper: bool = True) -> None:
        self.transport = transport
        self.paper = paper
        self.budget = RateBudget(capacity=100.0, per_second=100.0)
        self.sent: list[dict] = []
        self._seq = 0

    def _write(self, method: str, path: str, body: Optional[dict] = None,
               params: Optional[dict] = None, *, cost: float) -> dict:
        self.sent.append({"method": method, "path": path, "body": body, "params": params})
        if self.paper:
            self._seq += 1
            return {"order_id": f"PAPER-{self._seq}", "remaining_count": (body or {}).get("count", "0.00")}
        try:
            require_live_execution_allowed()
        except LiveExecutionBlocked:
            raise
        if self.transport is None:
            raise LiveExecutionBlocked("no transport")
        return self.transport.request(method, path, body=body, params=params)

    def place(self, market: str, side: Side, price_cents: int, size: float, *,
              best_opposing_bid_cents: Optional[int],
              client_order_id: str = "",
              order_group_id: str = "",
              now: float = 0.0) -> dict:
        if not self.budget.allow(CREATE_TOKENS, now):
            return {"ok": False, "error": "rate_budget"}
        coid = client_order_id or f"LIP-{uuid.uuid4().hex[:16]}"
        try:
            legacy = build_limit_order(
                ticker=market, side=side.value, price_cents=int(price_cents),
                size_contracts=int(round(size)), client_order_id=coid,
                best_opposing_bid_cents=best_opposing_bid_cents,
                time_in_force="good_till_canceled",
            )
            body = to_event_order_v2(legacy, order_group_id=order_group_id or None)
        except MakerSafetyError as e:
            return {"ok": False, "error": f"maker_safety: {e}"}
        try:
            resp = self._write("POST", CREATE, body, cost=CREATE_TOKENS)
        except LiveExecutionBlocked as e:
            return {"ok": False, "error": f"live_blocked: {e}"}
        return {"ok": True, "order_id": resp.get("order_id", ""), "client_order_id": coid,
                "body": body, "raw": resp}

    def decrease(self, order_id: str, reduce_to: float, *, market: str = "") -> dict:
        body = {"reduce_to": f"{float(reduce_to):.2f}"}
        path = f"{CREATE}/{order_id}/decrease"
        try:
            resp = self._write("POST", path, body, cost=CREATE_TOKENS)
        except LiveExecutionBlocked as e:
            return {"ok": False, "error": f"live_blocked: {e}"}
        return {"ok": True, "queue_preserved": True, "raw": resp}

    def amend(self, order_id: str, *, market: str, side: Side, price_cents: int,
              total_count: float, client_order_id: str = "") -> dict:
        """Price change or size-up. Queue is NOT preserved (Kalshi docs)."""
        legacy_side_price = price_cents
        # Rebuild the YES-book side the same way create does.
        try:
            legacy = build_limit_order(
                ticker=market, side=side.value, price_cents=int(price_cents),
                size_contracts=max(1, int(round(total_count))),
                client_order_id=client_order_id or "amend",
                best_opposing_bid_cents=0,   # amend of an already-resting order
                enforce_non_crossing=False,  # the exchange post_only flag still set
            )
            v2 = to_event_order_v2(legacy)
        except MakerSafetyError as e:
            return {"ok": False, "error": str(e)}
        body = {
            "ticker": market,
            "side": v2["side"],
            "price": v2["price"],
            "count": f"{float(total_count):.2f}",
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        # silence unused
        _ = legacy_side_price
        try:
            resp = self._write("POST", f"{CREATE}/{order_id}/amend", body, cost=CREATE_TOKENS)
        except LiveExecutionBlocked as e:
            return {"ok": False, "error": f"live_blocked: {e}"}
        return {"ok": True, "queue_preserved": False, "raw": resp}

    def cancel(self, order_id: str, *, market: str, now: float = 0.0) -> dict:
        if not self.budget.allow(CANCEL_TOKENS, now):
            return {"ok": False, "error": "rate_budget"}
        # market_ticker is required for auto-routing when exchange_index is omitted.
        path = f"{CREATE}/{order_id}?{urlencode({'market_ticker': market})}"
        try:
            resp = self._write("DELETE", path, cost=CANCEL_TOKENS)
        except LiveExecutionBlocked as e:
            return {"ok": False, "error": f"live_blocked: {e}"}
        return {"ok": True, "raw": resp}

    def cancel_all(self, orders: list[tuple[str, str]]) -> int:
        """Cancel (order_id, market) pairs. Paper counts them; live is blocked
        inside ``cancel`` until the maker interlock is verified."""
        n = 0
        for oid, market in orders:
            if self.cancel(oid, market=market).get("ok"):
                n += 1
        return n

    def create_order_group(self, contracts_limit: int) -> dict:
        """Exchange-side auto-cancel when the rolling 15-second fill count
        exceeds ``contracts_limit``. See docs.kalshi.com/getting_started/order_groups.
        """
        limit = max(1, min(1_000_000, int(contracts_limit)))
        body = {"contracts_limit": limit}
        try:
            resp = self._write("POST", "/portfolio/order_groups/create", body, cost=CREATE_TOKENS)
        except LiveExecutionBlocked as e:
            return {"ok": False, "error": f"live_blocked: {e}"}
        return {"ok": True, "contracts_limit": limit,
                "order_group_id": resp.get("order_group_id", f"PAPER-OG-{limit}"),
                "raw": resp}

    def queue_position(self, order_id: str) -> Optional[float]:
        """Contracts ahead of this order. GET is a read; paper returns None
        (we do not invent a queue we have not observed)."""
        if self.paper or self.transport is None:
            return None
        resp = self.transport.request(
            "GET", f"/portfolio/orders/{order_id}/queue_position")
        raw = resp.get("queue_position_fp")
        return parse_fp(raw) if raw is not None else None

    @staticmethod
    def parse_resting(raw: dict) -> Optional[VenueOrderView]:
        oid = str(raw.get("order_id") or "")
        if not oid:
            return None
        side_raw = str(raw.get("side") or "")
        if side_raw in ("bid", "yes"):
            side = Side.YES
            price = raw.get("price") or raw.get("yes_price_dollars")
        elif side_raw == "ask":
            side = Side.NO
            # YES ask at p is a NO bid at 1-p.
            price = raw.get("price")
            if price is not None:
                price = 1.0 - float(price)
            else:
                price = raw.get("no_price_dollars")
        elif side_raw == "no":
            side = Side.NO
            price = raw.get("no_price_dollars") or raw.get("price")
        else:
            return None
        if price is None:
            return None
        remaining = raw.get("remaining_count_fp", raw.get("remaining_count", raw.get("remaining")))
        if remaining is None:
            return None
        return VenueOrderView(
            order_id=oid,
            client_order_id=str(raw.get("client_order_id") or ""),
            market=str(raw.get("ticker") or raw.get("market_ticker") or ""),
            side=side,
            price_cents=dollars_to_cents(price) if float(price) <= 1.5 else int(price),
            remaining=parse_fp(remaining),
            status=str(raw.get("status") or "resting"),
        )
