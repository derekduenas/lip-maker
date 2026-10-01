"""Order state machine and venue reconciliation.

The matching loop is the only writer. ``transition`` refuses illegal jumps.
``reconcile`` is the exception: the venue snapshot is authoritative, so a
local PENDING_NEW that the venue has never heard of becomes REJECTED, and a
local RESTING order missing from the venue becomes CANCELLED (it filled or
was cancelled somewhere we did not see). Quantity and price on a resting
order are overwritten from the venue; we do not average them.
"""
from __future__ import annotations

import threading
from typing import Optional

from mm.types import (
    ManagedOrder, OrderState, ReconcileReport, VenueOrderView, can_transition,
)


class IllegalTransition(RuntimeError):
    pass


class OrderBook:
    """In-memory orders for one venue, guarded by a lock (single writer)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_client: dict[str, ManagedOrder] = {}
        self._by_venue_id: dict[str, str] = {}

    def get(self, client_order_id: str) -> Optional[ManagedOrder]:
        with self._lock:
            return self._by_client.get(client_order_id)

    def resting(self, market: str | None = None) -> list[ManagedOrder]:
        with self._lock:
            rows = [
                o for o in self._by_client.values()
                if o.state in (OrderState.RESTING, OrderState.PARTIAL,
                               OrderState.PENDING_AMEND, OrderState.PENDING_CANCEL,
                               OrderState.UNKNOWN, OrderState.PENDING_NEW)
            ]
        if market is None:
            return rows
        return [o for o in rows if o.market == market]

    def add(self, order: ManagedOrder) -> None:
        with self._lock:
            if order.client_order_id in self._by_client:
                raise IllegalTransition(
                    f"duplicate client_order_id {order.client_order_id}")
            self._by_client[order.client_order_id] = order
            if order.order_id:
                self._by_venue_id[order.order_id] = order.client_order_id

    def transition(self, client_order_id: str, dst: OrderState, *,
                   ts: float, order_id: str = "", remaining: float | None = None,
                   price_cents: int | None = None,
                   queue_preserved: bool | None = None) -> ManagedOrder:
        with self._lock:
            order = self._by_client.get(client_order_id)
            if order is None:
                raise IllegalTransition(f"unknown order {client_order_id}")
            if not can_transition(order.state, dst):
                raise IllegalTransition(
                    f"{order.state.value} -> {dst.value} is not a legal transition")
            order.state = dst
            order.updated_ts = ts
            if order_id:
                order.order_id = order_id
                self._by_venue_id[order_id] = client_order_id
            if remaining is not None:
                order.remaining = remaining
            if price_cents is not None:
                order.price_cents = price_cents
            if queue_preserved is not None:
                order.queue_preserved = queue_preserved
            return order

    def reconcile(self, venue_orders: list[VenueOrderView], *,
                  ts: float) -> ReconcileReport:
        """Make the local book match the venue. Venue wins.

        Orders in a terminal local state are left alone. Live orders the
        venue does not list are marked cancelled. Venue orders we have never
        seen are adopted as RESTING with state UNKNOWN origin (client id
        kept when the venue echoes it).
        """
        report = ReconcileReport()
        with self._lock:
            by_venue = {v.order_id: v for v in venue_orders if v.order_id}
            by_client_echo = {
                v.client_order_id: v for v in venue_orders if v.client_order_id
            }
            seen_clients: set[str] = set()
            for coid, order in list(self._by_client.items()):
                if order.state in (OrderState.FILLED, OrderState.CANCELLED,
                                   OrderState.REJECTED):
                    continue
                view = None
                if order.order_id and order.order_id in by_venue:
                    view = by_venue[order.order_id]
                elif coid in by_client_echo:
                    view = by_client_echo[coid]
                if view is None:
                    if order.state == OrderState.PENDING_NEW:
                        order.state = OrderState.REJECTED
                    else:
                        order.state = OrderState.CANCELLED
                    order.updated_ts = ts
                    report.gone.append(coid)
                    continue
                seen_clients.add(coid)
                changed = False
                if view.order_id and order.order_id != view.order_id:
                    order.order_id = view.order_id
                    self._by_venue_id[view.order_id] = coid
                    changed = True
                if order.remaining != view.remaining or order.price_cents != view.price_cents:
                    order.remaining = view.remaining
                    order.price_cents = view.price_cents
                    changed = True
                if view.remaining <= 0 or view.status in ("filled", "executed"):
                    order.state = OrderState.FILLED
                    changed = True
                elif order.state in (OrderState.PENDING_NEW, OrderState.UNKNOWN,
                                      OrderState.PENDING_AMEND, OrderState.PENDING_CANCEL):
                    order.state = OrderState.PARTIAL if order.filled > 0 else OrderState.RESTING
                    changed = True
                order.updated_ts = ts
                (report.overwritten if changed else report.unchanged).append(coid)

            for view in venue_orders:
                if view.client_order_id and view.client_order_id in self._by_client:
                    continue
                if view.order_id and view.order_id in self._by_venue_id:
                    continue
                if view.remaining <= 0:
                    continue
                coid = view.client_order_id or f"adopted-{view.order_id}"
                if coid in self._by_client:
                    continue
                adopted = ManagedOrder(
                    client_order_id=coid,
                    venue=self._guess_venue(),
                    market=view.market,
                    side=view.side,
                    price_cents=view.price_cents,
                    remaining=view.remaining,
                    state=OrderState.RESTING,
                    order_id=view.order_id,
                    paper=False,
                    updated_ts=ts,
                )
                # Venue of an adopted order is filled by the caller via the
                # book they passed in; default below is overwritten if the
                # book was constructed for a known venue.
                self._by_client[coid] = adopted
                if view.order_id:
                    self._by_venue_id[view.order_id] = coid
                report.adopted.append(coid)
        return report

    def _guess_venue(self):
        from mm.types import VenueName
        for o in self._by_client.values():
            return o.venue
        return VenueName.KALSHI
