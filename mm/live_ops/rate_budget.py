"""Local token-bucket rate budget with a reserved cancel lane.

Kalshi limits are token buckets (search snippet of docs.kalshi.com/getting_started/rate_limits,
VERIFY): default request cost 10 tokens, about 3 s of burst, tiers (read/write tokens per second)
Basic 200/100, Advanced 300/300, Premier 1000/1000, Paragon 2000/2000, Prime 4000/4000,
Prestige 10000/8000. We spend at ``fraction`` (0.7) of the limit and keep ``cancel_reserve``
(25%) of the write budget for cancels: a cancel may use the reserve and then the general
write bucket, a new order may only use the general bucket, so a burst of quotes can never
starve the ability to pull them. Non-blocking: acquire() answers now, wait_time() says how long.
"""
from __future__ import annotations

import time
from typing import Callable

TIERS = {"basic": (200, 100), "advanced": (300, 300), "premier": (1000, 1000),
         "paragon": (2000, 2000), "prime": (4000, 4000), "prestige": (10000, 8000)}
DEFAULT_COST = 10


class _Bucket:
    def __init__(self, rate: float, burst_s: float, clock) -> None:
        self.rate = float(rate)
        self.capacity = max(float(rate) * float(burst_s), 0.0)
        self.tokens = self.capacity
        self._clock = clock
        self._t = clock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._t) * self.rate)
        self._t = now

    def take(self, cost: float) -> bool:
        self._refill()
        if cost <= self.tokens + 1e-9:
            self.tokens -= cost
            return True
        return False

    def wait(self, cost: float) -> float:
        self._refill()
        if cost > self.capacity + 1e-9 or self.rate <= 0:
            return float("inf")
        return max(0.0, (cost - self.tokens) / self.rate)


class RateBudget:
    def __init__(self, tier: str = "basic", *, fraction: float = 0.7, cancel_reserve: float = 0.25,
                 burst_s: float = 3.0, clock: Callable[[], float] = time.monotonic) -> None:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}; one of {sorted(TIERS)}")
        if not 0 < fraction <= 1 or not 0 <= cancel_reserve < 1:
            raise ValueError("fraction must be in (0, 1], cancel_reserve in [0, 1)")
        read_tps, write_tps = TIERS[tier]
        write_total = write_tps * fraction
        self._b = {"read": _Bucket(read_tps * fraction, burst_s, clock),
                   "write": _Bucket(write_total * (1.0 - cancel_reserve), burst_s, clock),
                   "cancel": _Bucket(write_total * cancel_reserve, burst_s, clock)}
        self._read_tps, self._write_tps = read_tps * fraction, write_total * (1.0 - cancel_reserve)
        self.denied = {"read": 0, "write": 0, "cancel": 0}

    def rates(self) -> dict:
        return {"read_tps": self._read_tps, "write_tps": self._write_tps,
                "cancel_tps": self._b["cancel"].rate}

    def _lanes(self, kind: str) -> list:
        if kind == "read":
            return ["read"]
        if kind == "write":
            return ["write"]
        if kind == "cancel":
            return ["cancel", "write"]
        raise ValueError(f"unknown kind {kind!r}; read, write or cancel")

    def acquire(self, kind: str, items: int = 1, cost: float = DEFAULT_COST) -> bool:
        total = float(cost) * max(1, int(items))
        for lane in self._lanes(kind):
            if self._b[lane].take(total):
                return True
        self.denied[kind] += 1
        return False

    def wait_time(self, kind: str, items: int = 1, cost: float = DEFAULT_COST) -> float:
        total = float(cost) * max(1, int(items))
        return min(self._b[lane].wait(total) for lane in self._lanes(kind))
