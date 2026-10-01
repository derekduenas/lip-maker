"""Every Kalshi order on this path belongs to an order group.

REST has no cancel-on-disconnect. The substitute is a group on the
market's shard plus ``PUT /portfolio/order_groups/{id}/trigger``.
Groups do not cross shards, so the key is ``(market, exchange_index)``.

This is opt-in. ``KalshiAdapter.place`` does not create a group by
itself, so existing callers that assert the first request is the order
keep that shape. ``SafeSender`` is the path that refuses to send without
a group.
"""
from __future__ import annotations

from typing import Optional

from mm.risk import order_group_contracts_limit
from mm.types import Side


class OrderGroupBook:
    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.groups: dict[tuple[str, int], str] = {}

    def group_for(self, market: str, exchange_index: int, *,
                  contracts_limit: int = 1000) -> dict:
        key = (market, int(exchange_index))
        existing = self.groups.get(key)
        if existing:
            return {"ok": True, "order_group_id": existing,
                    "exchange_index": int(exchange_index)}
        limit = order_group_contracts_limit(contracts_limit)
        result = self.adapter.create_order_group(
            limit, market=market, exchange_index=int(exchange_index))
        gid = str(result.get("order_group_id") or "")
        if result.get("ok") and gid:
            self.groups[key] = gid
            result["order_group_id"] = gid
        return result


class SafeSender:
    """Place only with an order-group id. Trigger flattens those groups."""

    def __init__(self, adapter, book: Optional[OrderGroupBook] = None) -> None:
        self.adapter = adapter
        self.book = book if book is not None else OrderGroupBook(adapter)

    def place(self, market: str, side: Side, price_cents: int, size: float, *,
              exchange_index: int, best_opposing_bid_cents: Optional[int],
              contracts_limit: int = 1000, now: float = 0.0) -> dict:
        if exchange_index is None:
            return {"ok": False, "error": "shard_unknown", "order_id": ""}
        spec = self.book.group_for(
            market, int(exchange_index), contracts_limit=contracts_limit)
        gid = str(spec.get("order_group_id") or "")
        if not spec.get("ok") or not gid:
            if spec.get("error"):
                return spec
            return {"ok": False, "error": "no_order_group", "order_id": ""}
        self.adapter.remember_shard(market, int(exchange_index))
        return self.adapter.place(
            market, side, price_cents, size,
            best_opposing_bid_cents=best_opposing_bid_cents,
            order_group_id=gid, now=now,
        )

    def trigger_all(self) -> list[dict]:
        out = []
        for (_market, idx), gid in list(self.book.groups.items()):
            out.append(self.adapter.trigger_order_group(gid, exchange_index=idx))
        return out
