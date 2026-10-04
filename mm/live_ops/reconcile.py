"""Scheduled reconciliation against venue truth, halting on UNEXPLAINED divergence.

``mm.order_machine.OrderBook.reconcile`` makes the venue authoritative but silently adopts
strangers and silently cancels vanished orders. A vanished or unknown order is exactly what a
live operator must hear about, so this layer classifies every difference first:

  explained   a recorded fill accounts for it; a cancel we requested has landed; a fresh
              pending_new the venue has not shown yet (grace)
  unexplained unknown_venue_order, missing_at_venue, qty_mismatch, price_mismatch,
              duplicate_client_order_id, lost_order (pending_new past the grace),
              position_mismatch (beyond tolerance)

A fill explains a difference only if it is NEW: fills already applied to the local order
(``ManagedOrder.filled``) or counted by an earlier run are not counted again. The caller keeps
``filled`` in step with the fills it applies.

More than ``max_unexplained`` (default 0) halts: ``on_halt(reason, report)`` is called (the
caller cancels everything and latches the kill). Venue truth is applied to the local book either
way, so a mass-cancel includes adopted strangers. A venue error is itself a halt, not a crash.
Callables only: no network code here.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from mm.order_machine import OrderBook
from mm.types import OrderState

_DEAD_STATUS = ("canceled", "cancelled", "expired", "rejected")
_LIVE = (OrderState.RESTING, OrderState.PARTIAL, OrderState.PENDING_AMEND,
         OrderState.PENDING_CANCEL, OrderState.UNKNOWN, OrderState.PENDING_NEW)


class Reconciler:
    def __init__(self, book: OrderBook, *, fetch_orders: Callable[[], list],
                 fetch_fills: Optional[Callable[[], list]] = None,
                 fetch_positions: Optional[Callable[[], dict]] = None,
                 local_positions: Optional[Callable[[], dict]] = None,
                 on_halt: Optional[Callable[[str, dict], None]] = None,
                 fill_grace_s: float = 30.0, max_unexplained: int = 0,
                 position_tolerance: float = 0.0, clock: Callable[[], float] = time.time) -> None:
        self.book = book
        self.fetch_orders = fetch_orders
        self.fetch_fills = fetch_fills
        self.fetch_positions = fetch_positions
        self.local_positions = local_positions
        self.on_halt = on_halt
        self.fill_grace_s = float(fill_grace_s)
        self.max_unexplained = int(max_unexplained)
        self.position_tolerance = float(position_tolerance)
        self._clock = clock
        self._fill_seen: dict = {}
        self._last_run: Optional[float] = None
        self.runs = 0
        self.halts = 0
        self.last_report: dict = {}

    def maybe_run(self, every_s: float = 60.0) -> Optional[dict]:
        now = self._clock()
        if self._last_run is not None and now - self._last_run < every_s:
            return None
        return self.run_once()

    def _fill_total(self, fills: list, order_id: str, coid: str) -> float:
        return sum(float(f.get("count") or 0.0) for f in fills
                   if (order_id and f.get("order_id") == order_id) or (coid and f.get("client_order_id") == coid))

    def run_once(self) -> dict:
        now = self._clock()
        self._last_run = now
        self.runs += 1
        rep = {"ts": now, "halt": False, "explained": [], "unexplained": [], "error": None}
        try:
            venue = list(self.fetch_orders())
            fills = list(self.fetch_fills()) if self.fetch_fills else []
            v_pos = dict(self.fetch_positions()) if self.fetch_positions else None
        except Exception as exc:
            rep.update(halt=True, error=f"{type(exc).__name__}: {str(exc)[:100]}")
            self._halt("venue_unreachable", rep)
            return rep
        venue = [v for v in venue if str(v.status).lower() not in _DEAD_STATUS]   # cancelled rows are not live orders
        by_id = {v.order_id: v for v in venue if v.order_id}
        by_coid = {v.client_order_id: v for v in venue if v.client_order_id}
        coid_rows: dict = {}
        for v in venue:
            if v.client_order_id and v.remaining > 0:
                coid_rows.setdefault(v.client_order_id, set()).add(v.order_id)

        def add(kind: str, key: str, ok: bool, detail: str = "") -> None:
            (rep["explained"] if ok else rep["unexplained"]).append({"kind": kind, "key": key, "detail": detail})

        known_ids, known_coids = set(), set()
        for o in self.book.resting():
            if o.state not in _LIVE:
                continue
            known_ids.add(o.order_id)
            known_coids.add(o.client_order_id)
            view = by_id.get(o.order_id) if o.order_id else None
            view = view or by_coid.get(o.client_order_id)
            total = self._fill_total(fills, o.order_id, o.client_order_id)
            filled = max(0.0, total - max(float(o.filled or 0.0), self._fill_seen.get(o.client_order_id, 0.0)))
            self._fill_seen[o.client_order_id] = max(total, self._fill_seen.get(o.client_order_id, 0.0))
            if view is None:
                if o.state == OrderState.PENDING_CANCEL:
                    add("cancel_landed", o.client_order_id, True)
                elif o.state == OrderState.PENDING_NEW:
                    fresh = now - float(o.updated_ts or 0.0) < self.fill_grace_s
                    add("pending_new" if fresh else "lost_order", o.client_order_id, fresh,
                        "" if fresh else f"pending_new for {now - float(o.updated_ts or 0.0):.0f}s")
                elif filled >= float(o.remaining) - 1e-9 and filled > 0:
                    add("filled", o.client_order_id, True)
                else:
                    add("missing_at_venue", o.client_order_id, False, f"new fills {filled:g} < remaining {o.remaining:g}")
            elif len(coid_rows.get(o.client_order_id, ())) > 1:
                add("duplicate_client_order_id", o.client_order_id, False,
                    f"{len(coid_rows[o.client_order_id])} venue orders carry this id")
            elif abs(float(view.remaining) - float(o.remaining)) > 1e-9:
                diff = float(o.remaining) - float(view.remaining)
                add("qty_change", o.client_order_id, diff > 0 and filled >= diff - 1e-9,
                    f"local {o.remaining:g} venue {view.remaining:g} fills {filled:g}")
                if rep["unexplained"] and rep["unexplained"][-1]["key"] == o.client_order_id:
                    rep["unexplained"][-1]["kind"] = "qty_mismatch"
            elif int(view.price_cents) != int(o.price_cents):
                add("price_mismatch", o.client_order_id, False, f"local {o.price_cents} venue {view.price_cents}")
        for v in venue:
            if v.remaining <= 0:
                continue
            if v.order_id in known_ids or (v.client_order_id and v.client_order_id in known_coids):
                continue
            add("unknown_venue_order", v.client_order_id or v.order_id, False,
                f"{v.market} {v.side.value if hasattr(v.side, 'value') else v.side} {v.remaining:g}@{v.price_cents}")
        if v_pos is not None and self.local_positions is not None:
            mine = dict(self.local_positions())
            for market in sorted(set(v_pos) | set(mine)):
                a, b = tuple(v_pos.get(market, (0.0, 0.0))), tuple(mine.get(market, (0.0, 0.0)))
                gap = max(abs(a[0] - b[0]), abs(a[1] - b[1]))
                add("position_mismatch" if gap > self.position_tolerance else "position_ok", market,
                    gap <= self.position_tolerance, f"venue {a} local {b}")
        rep["explained"] = [e for e in rep["explained"] if e["kind"] != "position_ok"]
        if len(self._fill_seen) > 5000:
            self._fill_seen = {k: v for k, v in self._fill_seen.items() if k in known_coids}
        # venue truth, applied regardless of the halt; a just-sent pending_new gets its grace
        self.book.reconcile(venue, ts=now, keep_pending_after=now - self.fill_grace_s)
        rep["halt"] = len(rep["unexplained"]) > self.max_unexplained
        if rep["halt"]:
            self._halt(f"{len(rep['unexplained'])} unexplained divergence(s): "
                       + ", ".join(sorted({u['kind'] for u in rep['unexplained']})), rep)
        self.last_report = rep
        return rep

    def _halt(self, reason: str, rep: dict) -> None:
        self.halts += 1
        self.last_report = rep
        if self.on_halt is not None:
            self.on_halt(reason, rep)

    def status(self) -> dict:
        return {"runs": self.runs, "halts": self.halts, "last_ts": self._last_run,
                "last_unexplained": len(self.last_report.get("unexplained", [])),
                "last_error": self.last_report.get("error")}
