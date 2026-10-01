"""What every venue adapter must be able to do.

Live transmission is not this module's decision. Adapters call
``execution.order_request.require_live_execution_allowed`` before any
non-paper write. Paper is the default.

ForecastEx is registered so a later member adapter can drop in. It does
not invent an API.
"""
from __future__ import annotations

from typing import Optional, Protocol

from mm.types import VenueOrderView


class TransportHTTPError(Exception):
    """A venue HTTP response that is not a success.

    Adapters turn this into a typed result. Callers must not treat it as
    ``ok`` with an empty order id.
    """

    def __init__(self, status: int, body=None, *, method: str = "", path: str = ""):
        self.status = int(status)
        self.body = body if isinstance(body, dict) else {}
        self.raw_body = body
        self.method = method
        self.path = path
        super().__init__(f"HTTP {self.status} {method} {path}")


class Transport(Protocol):
    def request(self, method: str, path: str, *,
                body: Optional[dict] = None,
                params: Optional[dict] = None) -> dict:
        ...


class RateBudget:
    """Token bucket mirroring an exchange's published write costs.

    Kalshi's Create Order (V2) error schema (fetched 2026-10-01) says the
    default cost is 10 tokens per request, and Cancel Order (V2) says 2
    tokens. PM US retail is a flat 20 requests/second per key
    (https://docs.polymarket.us/api-reference/rate-limits, fetched 2026-10-01),
    modelled here as 1 token per request with capacity 20 per second.
    """

    def __init__(self, *, capacity: float, per_second: float) -> None:
        self.capacity = float(capacity)
        self.per_second = float(per_second)
        self.tokens = float(capacity)
        self._ts: float | None = None

    def allow(self, cost: float, now: float) -> bool:
        if self._ts is not None:
            self.tokens = min(self.capacity,
                              self.tokens + (now - self._ts) * self.per_second)
        self._ts = now
        if self.tokens < cost:
            return False
        self.tokens -= cost
        return True


def cents_to_dollars(cents: int) -> str:
    return f"{int(cents) / 100:.4f}"


def dollars_to_cents(value) -> int:
    return int(round(float(value) * 100))


def parse_fp(value) -> float:
    return float(value)
