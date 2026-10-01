"""Kalshi adapter on the V2 event-order API.

Paths, fetched 2026-10-01 from docs.kalshi.com (OpenAPI 3.32.0):

    POST   /portfolio/events/orders                         create
    DELETE /portfolio/events/orders/{order_id}              cancel
    POST   /portfolio/events/orders/{order_id}/decrease     size down
    POST   /portfolio/events/orders/{order_id}/amend        price or size
    GET    /portfolio/orders/{order_id}/queue_position      queue ahead
    POST   /portfolio/order_groups/create                   rolling 15s cap
    GET    /markets/{ticker}                                exchange_index
    GET    /portfolio/balance?exchange_index=               per-shard cash

The legacy ``/portfolio/orders`` mutation routes are deprecated no earlier
than 6 May 2026 (Create Order V2 description) and, per the changelog, were
scheduled to start rejecting between 18 and 25 June 2026. This adapter does
not call them.

Book side is the YES leg only. A NO buy at q cents is an ask to sell YES at
``1 - q/100``. GET /portfolio/orders still returns legacy ``side: yes`` for
that order; ``book_side`` / ``outcome_side`` are the fields that say which
contract it is (demo wire, 2026-09-30).

Shard routing (https://docs.kalshi.com/getting_started/exchange_sharding,
fetched 2026-10-01): ``exchange_index`` on the market is authoritative.
Omitting it defaults the write to shard 0 unless ``market_ticker`` (or
``ticker``) is present to auto-route. Order groups do not cross shards, and
collateral is per shard (``GET /portfolio/balance?exchange_index=``).
"""
from __future__ import annotations

import time
import uuid
from typing import Optional
from urllib.parse import urlencode

from execution.order_request import (
    CONTRACT_CENTS, LiveExecutionBlocked, MakerSafetyError, build_limit_order,
    require_live_execution_allowed, to_event_order_v2,
)
from mm.types import Side, VenueName, VenueOrderView
from mm.venues.base import (
    RateBudget, TransportHTTPError, backoff_seconds, dollars_to_cents, parse_fp,
)

CREATE = "/portfolio/events/orders"
CANCEL_TOKENS = 2.0
CREATE_TOKENS = 10.0


def _to_cents(price) -> int:
    return dollars_to_cents(price) if float(price) <= 1.5 else int(price)


def _fp(value, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def amend_response_killed_quote(resp: dict) -> bool:
    """HTTP 200 amend that cancelled the order instead of crossing.

    Demo wire 2026-09-30: amending a resting post_only order onto the ask
    returned 200 with ``remaining_count`` ``0.00`` and ``fill_count`` ``0.00``,
    and the order status became ``canceled``. That is not a live quote.
    """
    if not isinstance(resp, dict) or "error" in resp:
        return False
    raw = resp.get("remaining_count", resp.get("remaining_count_fp"))
    if raw is None:
        return False
    try:
        remaining = float(raw)
        fill = _fp(resp.get("fill_count", resp.get("fill_count_fp")), 0.0)
    except (TypeError, ValueError):
        return False
    return remaining == 0.0 and fill == 0.0


def error_envelope(body) -> tuple[str, str, str]:
    if not isinstance(body, dict):
        return "", "", ""
    err = body.get("error")
    if isinstance(err, dict):
        return (str(err.get("code") or ""),
                str(err.get("message") or ""),
                str(err.get("details") or ""))
    if isinstance(err, str) and err:
        return err, err, ""
    return (str(body.get("code") or ""),
            str(body.get("message") or ""),
            str(body.get("details") or ""))


def map_exchange_failure(body, *, operation: str, status: Optional[int]) -> Optional[dict]:
    """Turn an exchange error into a result dict.

    Returns None when ``body`` is not an error. A cancel whose order is
    already gone (HTTP 404, and not ``insufficient_shard_balance``) is
    success: there is nothing left to cancel. ``post only cross`` and
    ``insufficient_shard_balance`` are never success, and never carry a
    usable order id.
    """
    code, message, details = error_envelope(body)
    blob = f"{code} {message} {details}".lower()
    has_error = bool(code or (status is not None and status >= 400))
    if not has_error:
        return None
    if "post only cross" in blob:
        return {"ok": False, "error": "post_only_cross",
                "code": code or "invalid_order", "status": status, "order_id": ""}
    if code == "insufficient_shard_balance" or "insufficient_shard_balance" in blob:
        return {"ok": False, "error": "insufficient_shard_balance",
                "code": "insufficient_shard_balance", "status": status, "order_id": ""}
    if operation == "cancel" and status == 404:
        return {"ok": True, "already_gone": True, "error": "", "status": 404}
    err = code or (f"http_{status}" if status else "exchange_error")
    return {"ok": False, "error": err, "code": code, "status": status, "order_id": ""}


def exchange_index_from_market_payload(payload: dict) -> Optional[int]:
    """``exchange_index`` from GET /markets, GET /markets/{ticker}, or an event."""
    if not isinstance(payload, dict):
        return None
    if "exchange_index" in payload and not isinstance(payload.get("exchange_index"), dict):
        return int(payload["exchange_index"])
    market = payload.get("market")
    if isinstance(market, dict) and "exchange_index" in market:
        return int(market["exchange_index"])
    markets = payload.get("markets")
    if isinstance(markets, list) and markets and isinstance(markets[0], dict):
        if "exchange_index" in markets[0]:
            return int(markets[0]["exchange_index"])
    return None


def resting_quote(raw: dict) -> Optional[tuple[str, int]]:
    """``(yes|no, price_cents)`` for one portfolio order row.

    Prefer ``book_side`` (``bid`` / ``ask`` on the YES book), then
    ``outcome_side``, then legacy ``side``. A YES ``ask`` at p is a NO bid
    at ``100 - p``. Demo GET rows for a V2 ask come back ``side: yes`` with
    ``book_side: ask`` and ``outcome_side: no``; reading ``side`` first
    labels that NO quote as YES.
    """
    if not isinstance(raw, dict):
        return None
    book = str(raw.get("book_side") or "").lower()
    outcome = str(raw.get("outcome_side") or "").lower()
    side_raw = str(raw.get("side") or "").lower()

    def yes_book_price():
        for key in ("price", "yes_price_dollars", "yes_price"):
            if raw.get(key) is not None:
                return raw.get(key)
        return None

    if book == "bid":
        px = yes_book_price()
        if px is None:
            return None
        return "yes", _to_cents(px)
    if book == "ask":
        px = yes_book_price()
        if px is not None:
            return "no", CONTRACT_CENTS - _to_cents(px)
        for key in ("no_price_dollars", "no_price"):
            if raw.get(key) is not None:
                return "no", _to_cents(raw[key])
        return None
    if outcome == "yes":
        px = yes_book_price()
        if px is None:
            return None
        return "yes", _to_cents(px)
    if outcome == "no":
        if raw.get("price") is None and raw.get("yes_price_dollars") is None and raw.get("yes_price") is None:
            for key in ("no_price_dollars", "no_price"):
                if raw.get(key) is not None:
                    return "no", _to_cents(raw[key])
        px = yes_book_price()
        if px is None:
            return None
        return "no", CONTRACT_CENTS - _to_cents(px)
    if side_raw in ("bid", "yes"):
        px = yes_book_price() or raw.get("yes_price_dollars")
        if px is None:
            return None
        return "yes", _to_cents(px)
    if side_raw == "ask":
        px = raw.get("price")
        if px is not None:
            return "no", CONTRACT_CENTS - _to_cents(px)
        if raw.get("no_price_dollars") is not None:
            return "no", _to_cents(raw["no_price_dollars"])
        return None
    if side_raw == "no":
        px = raw.get("no_price_dollars") or raw.get("no_price") or raw.get("price")
        if px is None:
            return None
        return "no", _to_cents(px)
    return None


class KalshiAdapter:
    name = VenueName.KALSHI

    def __init__(self, transport: Optional[object] = None, *, paper: bool = True) -> None:
        from mm.venues.readonly import reject_market_data_reader
        reject_market_data_reader(transport)
        self.transport = transport
        self.paper = paper
        self.budget = RateBudget(capacity=100.0, per_second=100.0)
        self.sent: list[dict] = []
        self._seq = 0
        self._shards: dict[str, int] = {}

    def remember_shard(self, market: str, exchange_index: int) -> None:
        """Record the market's shard. ``GET /markets`` is the authority."""
        self._shards[market] = int(exchange_index)

    def _ensure_live(self) -> Optional[dict]:
        if self.paper:
            return None
        try:
            require_live_execution_allowed(venue="kalshi")
        except LiveExecutionBlocked as e:
            return {"ok": False, "error": f"live_blocked: {e}"}
        if self.transport is None:
            return {"ok": False, "error": "live_blocked: no transport"}
        return None

    def _write(self, method: str, path: str, body: Optional[dict] = None,
               params: Optional[dict] = None, *, cost: float) -> dict:
        self.sent.append({"method": method, "path": path, "body": body, "params": params})
        if self.paper:
            self._seq += 1
            remaining = (body or {}).get("count", (body or {}).get("reduce_to", "0.00"))
            return {"order_id": f"PAPER-{self._seq}", "remaining_count": remaining}
        require_live_execution_allowed(venue="kalshi")
        if self.transport is None:
            raise LiveExecutionBlocked("no transport")
        from mm.venues.readonly import reject_market_data_reader
        reject_market_data_reader(self.transport)
        return self.transport.request(method, path, body=body, params=params)

    def _call(self, method: str, path: str, body: Optional[dict] = None,
              params: Optional[dict] = None, *, cost: float, operation: str) -> dict:
        blocked = self._ensure_live()
        if blocked:
            return blocked
        try:
            resp = self._write(method, path, body, params, cost=cost)
        except TransportHTTPError as e:
            mapped = map_exchange_failure(e.body, operation=operation, status=e.status)
            if mapped is None:
                mapped = {"ok": False, "error": f"http_{e.status}", "status": e.status, "order_id": ""}
            wait = backoff_seconds(e.status, 0)
            if wait is not None:
                mapped["error"] = "rate_limited"
                mapped["backoff_s"] = wait
            mapped["raw"] = e.body
            return mapped
        except LiveExecutionBlocked as e:
            return {"ok": False, "error": f"live_blocked: {e}"}
        return self._finish(operation, resp if isinstance(resp, dict) else {})

    def _finish(self, operation: str, resp: dict) -> dict:
        mapped = map_exchange_failure(resp, operation=operation, status=None)
        if mapped is not None:
            mapped["raw"] = resp
            return mapped
        if operation == "amend" and amend_response_killed_quote(resp):
            return {
                "ok": False,
                "dead": True,
                "error": "post_only_cancelled",
                "order_id": str(resp.get("order_id") or ""),
                "remaining": 0.0,
                "queue_preserved": False,
                "status": "canceled",
                "raw": resp,
            }
        if operation == "place":
            oid = str(resp.get("order_id") or "")
            if not oid:
                return {"ok": False, "error": "missing_order_id", "order_id": "", "raw": resp}
            return {"ok": True, "order_id": oid, "raw": resp}
        if operation == "decrease":
            return {"ok": True, "queue_preserved": True,
                    "order_id": str(resp.get("order_id") or ""), "raw": resp}
        if operation == "amend":
            return {"ok": True, "queue_preserved": False, "dead": False,
                    "order_id": str(resp.get("order_id") or ""), "raw": resp}
        if operation == "cancel":
            return {"ok": True, "order_id": str(resp.get("order_id") or ""), "raw": resp}
        if operation == "order_group":
            limit_echo = resp.get("contracts_limit")
            return {
                "ok": True,
                "order_group_id": str(resp.get("order_group_id") or ""),
                "exchange_index": resp.get("exchange_index"),
                "raw": resp,
                "contracts_limit": limit_echo,
            }
        if operation == "trigger":
            return {"ok": True, "order_group_id": str(resp.get("order_group_id") or ""),
                    "raw": resp}
        return {"ok": True, "raw": resp}

    def resolve_shard(self, market: str) -> Optional[int]:
        if market in self._shards:
            return self._shards[market]
        if self.paper or self.transport is None:
            return None
        try:
            payload = self.transport.request("GET", f"/markets/{market}")
        except TransportHTTPError:
            return None
        idx = exchange_index_from_market_payload(payload)
        if idx is not None:
            self._shards[market] = idx
        return idx

    def shard_balance_usd(self, exchange_index: int) -> dict:
        """Available dollars on one shard.

        ``GET /portfolio/balance?exchange_index=`` scopes both ``balance`` and
        ``balance_dollars`` to that shard (Get Balance, fetched 2026-10-01).
        Omitting the parameter mixes every shard into one number, which is
        not spendable on shard 2 or 3.
        """
        if self.paper or self.transport is None:
            return {"ok": False, "error": "no_balance_read"}
        try:
            payload = self.transport.request(
                "GET", "/portfolio/balance",
                params={"exchange_index": int(exchange_index)},
            )
        except TransportHTTPError as e:
            mapped = map_exchange_failure(e.body, operation="balance", status=e.status)
            return mapped or {"ok": False, "error": f"http_{e.status}", "status": e.status}
        from execution.kalshi_auth import parse_balance_usd
        available = parse_balance_usd(payload)
        return {"ok": True, "available_usd": available, "exchange_index": int(exchange_index)}

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
        if not self.paper:
            blocked = self._ensure_live()
            if blocked:
                return blocked
            idx = self.resolve_shard(market)
            if idx is None:
                return {"ok": False, "error": "shard_unknown", "order_id": ""}
            need = (int(price_cents) / 100.0) * float(size)
            bal = self.shard_balance_usd(idx)
            if not bal.get("ok"):
                out = {"ok": False, "error": bal.get("error") or "shard_balance_unavailable",
                       "order_id": ""}
                out.update({k: v for k, v in bal.items() if k != "ok"})
                return out
            if float(bal["available_usd"]) + 1e-9 < need:
                return {
                    "ok": False,
                    "error": "insufficient_shard_balance",
                    "code": "insufficient_shard_balance",
                    "exchange_index": idx,
                    "available_usd": bal["available_usd"],
                    "need_usd": need,
                    "order_id": "",
                }
            body["exchange_index"] = int(idx)
        else:
            remembered = self._shards.get(market)
            if remembered is not None:
                body["exchange_index"] = int(remembered)
        result = self._call("POST", CREATE, body, cost=CREATE_TOKENS, operation="place")
        result["body"] = body
        result["client_order_id"] = coid
        return result

    def _budget_ok(self, cost: float, now: float | None) -> bool:
        ts = time.time() if now is None else float(now)
        return self.budget.allow(cost, ts)

    def decrease(self, order_id: str, reduce_to: float, *, market: str = "",
                 exchange_index: Optional[int] = None,
                 now: float | None = None) -> dict:
        """Size down. ``market_ticker`` is what routes off shard 0.

        Decrease Order V2 (fetched 2026-10-01) takes ``market_ticker`` and
        ``exchange_index`` on the body. Omitting both defaults to shard 0.
        """
        if not market:
            return {"ok": False, "error": "market_ticker_required", "order_id": ""}
        if not self._budget_ok(CREATE_TOKENS, now):
            return {"ok": False, "error": "rate_budget", "order_id": ""}
        body = {"reduce_to": f"{float(reduce_to):.2f}", "market_ticker": market}
        idx = exchange_index if exchange_index is not None else self._shards.get(market)
        if idx is not None:
            body["exchange_index"] = int(idx)
        return self._call("POST", f"{CREATE}/{order_id}/decrease", body,
                          cost=CREATE_TOKENS, operation="decrease")

    def amend(self, order_id: str, *, market: str, side: Side, price_cents: int,
              total_count: float, client_order_id: str = "",
              exchange_index: Optional[int] = None,
              now: float | None = None) -> dict:
        """Price change or size-up. Queue is NOT preserved (Kalshi docs).

        A 200 with remaining 0 and fill 0 is the exchange cancelling a
        post_only amend that would have crossed. That quote is dead.
        """
        try:
            legacy = build_limit_order(
                ticker=market, side=side.value, price_cents=int(price_cents),
                size_contracts=max(1, int(round(total_count))),
                client_order_id=client_order_id or "amend",
                best_opposing_bid_cents=0,
                enforce_non_crossing=False,
            )
            v2 = to_event_order_v2(legacy)
        except MakerSafetyError as e:
            return {"ok": False, "error": str(e)}
        if not self._budget_ok(CREATE_TOKENS, now):
            return {"ok": False, "error": "rate_budget", "order_id": ""}
        body = {
            "ticker": market,
            "side": v2["side"],
            "price": v2["price"],
            "count": f"{float(total_count):.2f}",
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        idx = exchange_index if exchange_index is not None else self._shards.get(market)
        if idx is not None:
            body["exchange_index"] = int(idx)
        return self._call("POST", f"{CREATE}/{order_id}/amend", body,
                          cost=CREATE_TOKENS, operation="amend")

    def cancel(self, order_id: str, *, market: str, now: float = 0.0) -> dict:
        if not self.budget.allow(CANCEL_TOKENS, now):
            return {"ok": False, "error": "rate_budget"}
        query = {"market_ticker": market}
        idx = self._shards.get(market)
        if idx is not None:
            query["exchange_index"] = idx
        path = f"{CREATE}/{order_id}?{urlencode(query)}"
        return self._call("DELETE", path, cost=CANCEL_TOKENS, operation="cancel")

    def cancel_all(self, orders: list[tuple[str, str]]) -> int:
        """Cancel (order_id, market) pairs. Paper counts them; live is blocked
        inside ``cancel`` until the Kalshi maker switch is acknowledged."""
        n = 0
        for oid, market in orders:
            if self.cancel(oid, market=market).get("ok"):
                n += 1
        return n

    def create_order_group(self, contracts_limit: int, *, market: str = "",
                           exchange_index: Optional[int] = None) -> dict:
        """Rolling 15s cap on the market's shard.

        Order groups do not function across exchange instances
        (exchange sharding docs). The create body defaults ``exchange_index``
        to 0, so a group for a shard-2 market has to name that shard.
        """
        limit = max(1, min(1_000_000, int(contracts_limit)))
        idx = exchange_index
        if not self.paper:
            blocked = self._ensure_live()
            if blocked:
                return blocked
            if idx is None and market:
                idx = self.resolve_shard(market)
            if idx is None:
                return {"ok": False, "error": "shard_unknown", "order_id": ""}
        elif idx is None and market:
            idx = self._shards.get(market)
        body = {"contracts_limit": limit}
        if idx is not None:
            body["exchange_index"] = int(idx)
        result = self._call("POST", "/portfolio/order_groups/create", body,
                            cost=CREATE_TOKENS, operation="order_group")
        if result.get("ok") and "contracts_limit" not in result:
            result["contracts_limit"] = limit
        elif result.get("ok"):
            result["contracts_limit"] = limit
        if result.get("ok") and not result.get("order_group_id"):
            result["order_group_id"] = f"PAPER-OG-{limit}"
        return result

    def trigger_order_group(self, order_group_id: str, *,
                            exchange_index: Optional[int] = None) -> dict:
        """Cancel every resting order in the group.

        ``PUT /portfolio/order_groups/{id}/trigger`` (fetched 2026-10-01).
        Groups do not cross shards, so the shard is sent when it is known.
        """
        params = None
        if exchange_index is not None:
            params = {"exchange_index": int(exchange_index)}
        path = f"/portfolio/order_groups/{order_group_id}/trigger"
        return self._call("PUT", path, params=params, cost=CANCEL_TOKENS,
                          operation="trigger")

    def queue_position(self, order_id: str, *, market: str = "") -> Optional[float]:
        """Contracts ahead of this order.

        Without ``market_ticker`` the read falls back to shard 0
        (demo wire, 2026-09-30). Paper returns None: we do not invent a queue.
        """
        if self.paper or self.transport is None or not market:
            return None
        params: dict = {"market_ticker": market}
        idx = self._shards.get(market)
        if idx is not None:
            params["exchange_index"] = idx
        try:
            resp = self.transport.request(
                "GET", f"/portfolio/orders/{order_id}/queue_position", params=params)
        except TransportHTTPError:
            return None
        raw = resp.get("queue_position_fp")
        return parse_fp(raw) if raw is not None else None

    @staticmethod
    def parse_resting(raw: dict) -> Optional[VenueOrderView]:
        oid = str(raw.get("order_id") or "")
        if not oid:
            return None
        quote = resting_quote(raw)
        if quote is None:
            return None
        side_name, cents = quote
        remaining = raw.get("remaining_count_fp", raw.get("remaining_count", raw.get("remaining")))
        if remaining is None:
            return None
        return VenueOrderView(
            order_id=oid,
            client_order_id=str(raw.get("client_order_id") or ""),
            market=str(raw.get("ticker") or raw.get("market_ticker") or ""),
            side=Side.YES if side_name == "yes" else Side.NO,
            price_cents=int(cents),
            remaining=parse_fp(remaining),
            status=str(raw.get("status") or "resting"),
        )
