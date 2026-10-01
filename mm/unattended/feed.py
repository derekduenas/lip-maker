"""Orderbook drive for requotes.

The WebSocket is primary. When its last message is older than
``stale_s``, the next read comes from REST. A reference-price move of
one cent or more calls the requote handler inline, so the decision is
not waiting on a timer. Nothing here opens a socket.
"""
from __future__ import annotations

from typing import Callable, Optional

from engine.lip_scorer import BookLevel, _find_cutoff_price

DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"


def reference_cents(bids: list[tuple[int, float]], target_size: float) -> Optional[int]:
    """First price where cumulative size reaches target/5. None if the book is short."""
    if target_size <= 0:
        return None
    levels = [BookLevel(int(price), float(size)) for price, size in bids if size > 0]
    levels.sort(key=lambda level: -level.price_cents)
    return _find_cutoff_price(levels, target_size / 5.0)


class RequoteGate:
    """Requote when the reference price moves by at least one tick.

    The handler runs before ``on_reference`` returns. ``latency`` is the
    queue delay, which is 0 for an inline callback. Callers still treat
    anything at or above 1 second as too slow to count.
    """

    def __init__(self, on_requote: Callable[[str, int, float], None], *,
                 min_ticks: int = 1) -> None:
        self.on_requote = on_requote
        self.min_ticks = int(min_ticks)
        self.last: dict[str, int] = {}

    def on_reference(self, market: str, price_cents: int, now: float) -> None:
        previous = self.last.get(market)
        self.last[market] = int(price_cents)
        if previous is None:
            return
        if abs(int(price_cents) - previous) < self.min_ticks:
            return
        self.on_requote(market, int(price_cents), 0.0)


class OrderbookFeed:
    def __init__(self, rest_fetch: Callable[[str], dict], *, stale_s: float = 1.0) -> None:
        self.rest_fetch = rest_fetch
        self.stale_s = float(stale_s)
        self.books: dict[str, dict] = {}
        self.ws_at: dict[str, float] = {}
        self.source: dict[str, str] = {}

    def note_ws(self, market: str, book: dict, now: float) -> None:
        self.books[market] = book
        self.ws_at[market] = float(now)
        self.source[market] = "ws"

    def read(self, market: str, now: float) -> dict:
        seen = self.ws_at.get(market)
        fresh = seen is not None and (float(now) - seen) <= self.stale_s
        if fresh:
            self.source[market] = "ws"
            return self.books[market]
        book = self.rest_fetch(market)
        self.books[market] = book
        self.source[market] = "rest"
        return book


class BookDriver:
    """Requote when either side's LIP reference moves by one cent."""

    def __init__(self, targets: dict[str, float], on_requote) -> None:
        self.targets = dict(targets)
        self.yes_gate = RequoteGate(on_requote)
        self.no_gate = RequoteGate(on_requote)

    def on_book(self, market: str, *, yes_bids, no_bids, now: float) -> None:
        target = float(self.targets.get(market, 0))
        yes_ref = reference_cents(list(yes_bids), target)
        no_ref = reference_cents(list(no_bids), target)
        if yes_ref is not None:
            self.yes_gate.on_reference(market, yes_ref, now)
        if no_ref is not None:
            self.no_gate.on_reference(market, no_ref, now)
