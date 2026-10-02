"""Polymarket US adapter.

Fetched 2026-10-01:

* Create carries ``participateDontInitiate: true`` so a crossing order is
  rejected instead of taking. Docs:
  https://docs.polymarket.us/api-reference/orders/create-multiple-orders
* Modify: POST /v1/order/{orderId}/modify
  https://docs.polymarket.us/api-reference/orders/modify-order
  The batch sibling is documented as cancel-replace. The single-modify page
  does not say the queue is kept, so this adapter never claims it is.
* Cancel all: POST /v1/orders/open/cancel
  https://docs.polymarket.us/api-reference/orders/cancel-all-open-orders
* Incentives: GET https://gateway.polymarket.us/v1/incentives at 5 req/s.
  https://docs.polymarket.us/api-reference/incentives/overview
* Retail rate limit: 20 requests/second per API key.
  https://docs.polymarket.us/api-reference/rate-limits

There is no PM US equivalent of a Kalshi order group in the pages above.
Cancel-all on disconnect is the protection this adapter exposes.

Price side. ``price.value`` on the wire is always the YES (long) price,
whatever the intent. https://docs.polymarket.us/api-reference/orders/overview
(fetched 2026-10-01): "The `price.value` field always represents the long
side's price, regardless of which order intent you use." and "To trade the
NO side at any price X, set `price.value = 1.00 - X`." (its table: buy Iowa,
the NO side, at 0.83 -> ORDER_INTENT_BUY_SHORT, price.value 0.17). The
concepts page (https://docs.polymarket.us/concepts/orders) says the same:
"when you place an order, the price always refers to the YES side".
Callers of this adapter pass the price of the outcome they buy (YES cents
for BUY_LONG, NO cents for BUY_SHORT); ``pmus_wire_price_cents`` converts.
"""
from __future__ import annotations

import time
from typing import Optional

from execution.order_request import LiveExecutionBlocked, require_live_execution_allowed
from mm.types import VenueName
from mm.venues.base import RateBudget, Transport

CREATE = "/v1/orders"

LONG_INTENTS = ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_LONG")
SHORT_INTENTS = ("ORDER_INTENT_BUY_SHORT", "ORDER_INTENT_SELL_SHORT")


def pmus_wire_price_cents(intent: str, price_cents: int) -> int:
    """YES-side ``price.value`` cents for an order on ``intent``.

    ``price_cents`` is the price of the outcome the intent trades: YES for
    the LONG intents, NO for the SHORT intents. A short-intent price X goes
    out as 100 - X (docs: "set `price.value = 1.00 - X`"). Raises
    ValueError for an unknown intent or a price outside 1..99.
    """
    p = int(price_cents)
    if not 1 <= p <= 99:
        raise ValueError(f"price_cents {price_cents} outside 1..99")
    if intent in LONG_INTENTS:
        return p
    if intent in SHORT_INTENTS:
        return 100 - p
    raise ValueError(f"unknown PM US intent {intent!r}")


class PMUSAdapter:
    name = VenueName.PMUS

    def __init__(self, transport: Optional[Transport] = None, *, paper: bool = True,
                 incentives: Optional[list] = None) -> None:
        self.transport = transport
        self.paper = paper
        self.budget = RateBudget(capacity=20.0, per_second=20.0)
        # Gateway incentives are documented at 5 requests/second, separate
        # from the 20/s retail order budget.
        self.incentive_budget = RateBudget(capacity=5.0, per_second=5.0)
        self.incentive_limited = False
        self.incentives_cache = list(incentives or [])
        self.sent: list[dict] = []
        self._seq = 0
        # order_id -> intent of orders this instance placed. ``modify`` needs
        # the intent to put the price on the YES side.
        self._intent_by_order: dict[str, str] = {}

    def _write(self, method: str, path: str, body: Optional[dict] = None, *,
               now: float) -> dict:
        if not self.budget.allow(1.0, now):
            return {"ok": False, "error": "rate_budget"}
        self.sent.append({"method": method, "path": path, "body": body})
        if self.paper:
            self._seq += 1
            return {"ok": True, "order_id": f"PM-PAPER-{self._seq}"}
        try:
            require_live_execution_allowed()
        except LiveExecutionBlocked as e:
            return {"ok": False, "error": f"live_blocked: {e}"}
        if self.transport is None:
            return {"ok": False, "error": "no transport"}
        raw = self.transport.request(method, path, body=body)
        return {"ok": True, "raw": raw, "order_id": raw.get("orderId") or raw.get("order_id", "")}

    def place(self, market_slug: str, *, intent: str, price_cents: int, quantity: float,
              now: float = 0.0) -> dict:
        """``intent`` is ORDER_INTENT_BUY_LONG (YES) or ORDER_INTENT_BUY_SHORT (NO).

        ``price_cents`` is the price of the outcome bought: YES cents for
        BUY_LONG, NO cents for BUY_SHORT. The body carries the YES-side
        value, so a NO bid at 40 is sent as price.value "0.60".
        """
        if intent not in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"):
            return {"ok": False, "error": f"intent {intent} is not a passive buy"}
        try:
            wire = pmus_wire_price_cents(intent, price_cents)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        body = {
            "marketSlug": market_slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{wire / 100:.2f}", "currency": "USD"},
            "quantity": float(quantity),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "participateDontInitiate": True,
        }
        resp = self._write("POST", CREATE, body, now=now)
        resp["body"] = body
        resp["queue_preserved"] = False
        resp["wire_price_cents"] = wire
        if intent == "ORDER_INTENT_BUY_SHORT":
            resp["no_price_cents"] = int(price_cents)
        if resp.get("ok") and resp.get("order_id"):
            self._intent_by_order[resp["order_id"]] = intent
        return resp

    def modify(self, order_id: str, *, price_cents: int, quantity: float,
               now: float = 0.0, intent: Optional[str] = None) -> dict:
        """Reprice an order. ``price_cents`` is in the order's outcome terms.

        The intent comes from ``intent`` or from this instance's own place().
        With neither, the price side is unknown and the modify is refused
        rather than guessed.
        """
        intent = intent or self._intent_by_order.get(order_id)
        if intent is None:
            return {"ok": False, "error": f"unknown intent for order {order_id}; "
                                          "price side is ambiguous, pass intent="}
        try:
            wire = pmus_wire_price_cents(intent, price_cents)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        body = {
            "price": {"value": f"{wire / 100:.2f}", "currency": "USD"},
            "quantity": float(quantity),
            "participateDontInitiate": True,
        }
        resp = self._write("POST", f"/v1/order/{order_id}/modify", body, now=now)
        resp["body"] = body
        resp["queue_preserved"] = False
        return resp

    def cancel_all(self, slugs: Optional[list[str]] = None, *, now: float = 0.0) -> dict:
        body = {"slugs": list(slugs or [])}
        return self._write("POST", "/v1/orders/open/cancel", body, now=now)

    def engine_place(self, market_slug: str, *, intent: str, price_cents: int,
                     quantity: float, now: float = 0.0) -> dict:
        """MM-engine entry. The PM US key is read-only, so this stays paper.

        A transport attached for signed reads is not used. The body is the
        same maker order ``place`` builds, including ``participateDontInitiate``.
        """
        # Latch paper on this instance. The key used for engine orders is
        # read-only; a later place() on the same object must not go live.
        self.paper = True
        resp = self.place(market_slug, intent=intent, price_cents=price_cents,
                          quantity=quantity, now=now)
        resp["read_only"] = True
        resp["paper"] = True
        return resp

    def incentives(self, now: float | None = None) -> list:
        """Programs last supplied by the caller or a gateway fetch.

        Paper does not call the network. A live fetch is a GET and does not
        go through the maker-only write interlock. It does consume the
        5/s incentive budget.
        """
        if self.incentives_cache or self.paper or self.transport is None:
            return list(self.incentives_cache)
        ts = time.time() if now is None else float(now)
        if not self.incentive_budget.allow(1.0, ts):
            self.incentive_limited = True
            return list(self.incentives_cache)
        self.incentive_limited = False
        raw = self.transport.request("GET", "/v1/incentives")
        self.incentives_cache = list(raw.get("markets") or raw.get("incentives") or [])
        return list(self.incentives_cache)
