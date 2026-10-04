"""Kalshi order groups as a server-side fill cap and mass-cancel (model + fake adapter).

From search snippets of docs.kalshi.com (VERIFY): a group has a contracts_limit over a rolling
15 s window; when the contracts matched exceed it, every resting order in the group is cancelled
and new orders are refused until the group is reset; lowering the limit can trigger it at once.
This module models that so the engine can size the limit from its caps and be tested against a
fake; it contains no network code.
"""
from __future__ import annotations

from collections import deque

MAX_LIMIT = 1_000_000


def clamp_limit(contracts: int) -> int:
    """Documented 1..1,000,000 range (mm.risk.order_group_contracts_limit)."""
    return max(1, min(MAX_LIMIT, int(contracts)))


class OrderGroupModel:
    def __init__(self, limit_contracts: int, window_s: float = 15.0) -> None:
        self.limit = clamp_limit(limit_contracts)
        self.window_s = float(window_s)
        self.members: set = set()
        self.triggered = False
        self._fills: deque = deque()

    def admit(self, client_order_id: str) -> bool:
        if self.triggered:
            return False
        self.members.add(client_order_id)
        return True

    def _total(self, ts: float) -> float:
        while self._fills and ts - self._fills[0][0] > self.window_s:
            self._fills.popleft()
        return sum(c for _t, c in self._fills)

    def _trip(self) -> list:
        self.triggered = True
        cancelled = sorted(self.members)
        self.members.clear()
        return cancelled

    def on_fill(self, count: float, ts: float) -> list:
        """Record matched contracts; returns the orders cancelled if the group tripped."""
        self._fills.append((float(ts), float(count)))
        if not self.triggered and self._total(ts) > self.limit:
            return self._trip()
        return []

    def update_limit(self, contracts: int, ts: float) -> list:
        self.limit = clamp_limit(contracts)
        if not self.triggered and self._total(ts) > self.limit:
            return self._trip()
        return []

    def reset(self) -> None:
        self.triggered = False
        self._fills.clear()


class FakeOrderGroupAdapter:
    """In-memory stand-in for the venue's order-group API (tests / demo)."""

    def __init__(self) -> None:
        self.groups: dict[str, OrderGroupModel] = {}
        self._n = 0

    def create(self, limit_contracts: int) -> str:
        self._n += 1
        gid = f"grp{self._n}"
        self.groups[gid] = OrderGroupModel(limit_contracts)
        return gid

    def add_order(self, gid: str, client_order_id: str) -> bool:
        return self.groups[gid].admit(client_order_id)

    def fill(self, gid: str, count: float, ts: float) -> list:
        return self.groups[gid].on_fill(count, ts)

    def update_limit(self, gid: str, contracts: int, ts: float) -> list:
        return self.groups[gid].update_limit(contracts, ts)

    def reset(self, gid: str) -> None:
        self.groups[gid].reset()
