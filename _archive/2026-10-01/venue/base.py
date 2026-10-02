"""Abstract Venue interface for INNAIT multi-venue stack.

Every venue (Kalshi, Gemini Predictions, future Polymarket offshore, etc.)
implements this interface. Shared code (quote_manager, adaptive_sizer,
reconciliation, toxicity_filter, futures_feed) consumes `Venue` not
venue-specific clients.

Design principles:
  - Prices in CENTS (int), not dollars (float) — avoids float precision bugs
  - Sizes in CONTRACTS (int) — matches Kalshi/Gemini semantics
  - Timestamps in ISO-8601 UTC strings — portable across platforms
  - Errors via Result types, not exceptions — forces callers to handle them
  - Async orderbook stream; sync REST — matches real API patterns

Adding a new venue (e.g., gemini.py) = subclass Venue + implement 8 methods.
Expected scope: 2-3 days for a venue with good SDK, 1 week for one without.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


# ─── Value Types ─────────────────────────────────────────────────────

@dataclass
class BookLevel:
    price_cents: int
    size: int


@dataclass
class OrderbookSnapshot:
    ticker: str
    yes_bids: list[BookLevel]   # sorted desc by price
    no_bids:  list[BookLevel]   # sorted desc by price
    captured_at: str            # ISO UTC


@dataclass
class Position:
    ticker:           str
    net_contracts:    int      # positive = long YES; negative = long NO
    avg_entry_cents:  Optional[int]
    market_exposure_usd: float  # cost basis of current open position
    realized_pnl_usd: float     # realized on this position (open-position-only per most APIs)


@dataclass
class Fill:
    trade_id:    str
    order_id:    str
    ticker:      str
    side:        str           # "yes" or "no"
    count:       int
    price_cents: int            # actual fill price on the relevant side
    is_maker:    bool           # True if we provided liquidity
    created_at:  str            # ISO UTC


@dataclass
class OrderResult:
    success:  bool
    order_id: Optional[str] = None
    error:    Optional[str] = None
    raw:      Any = None        # venue-specific response for debugging


@dataclass
class MarketMetadata:
    ticker:           str
    tick_size_cents:  int
    min_size:         int
    close_time:       Optional[str]        # ISO UTC expiration/settle
    settlement_rule:  Optional[str]        # raw human-readable if venue provides
    extra:            dict = field(default_factory=dict)  # venue-specific


# ─── Abstract Venue ───────────────────────────────────────────────────

class Venue(ABC):
    """Implement this interface for each new venue.

    Conventions:
      - All methods that hit the network can raise on network failure.
        Callers should handle. place_order and cancel_* return Result types
        for expected business failures (already-filled, insufficient balance).
      - side is always lowercase "yes" or "no".
      - Prices ALWAYS in cents integers.
    """

    name: str  # short venue identifier: "kalshi", "gemini", "polymarket"

    # ── Account state ──
    @abstractmethod
    def get_balance(self) -> float:
        """Cash balance in USD. For USDC venues, the USD-equivalent."""
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """All open positions. Empty list if none."""
        ...

    @abstractmethod
    def get_fills(self, since_iso: Optional[str] = None,
                  limit: int = 200) -> list[Fill]:
        """Fills ordered newest-first. If since_iso provided, only fills created_at >= since_iso."""
        ...

    # ── Orders ──
    @abstractmethod
    def place_order(self, ticker: str, side: str, price_cents: int,
                    size_contracts: int, post_only: bool = True) -> OrderResult:
        """Place a maker-only (post_only) limit order.

        If post_only=True and the order would cross the spread, venue should
        reject without filling. Returns OrderResult with order_id on success.
        """
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel by order_id. Returns True on success (or already-cancelled).
        False on actual failure."""
        ...

    @abstractmethod
    def cancel_all_for_ticker(self, ticker: str) -> int:
        """Cancel all resting orders for a specific ticker.
        Returns count cancelled."""
        ...

    # ── Market data ──
    @abstractmethod
    def get_market_metadata(self, ticker: str) -> MarketMetadata:
        """Static market info needed for quoting."""
        ...

    @abstractmethod
    async def stream_orderbook(self, tickers: list[str],
                                on_update: Callable[[OrderbookSnapshot], Awaitable[None]]
                               ) -> None:
        """WebSocket orderbook stream. Calls on_update per book change.

        Must handle reconnects transparently. Raises only on unrecoverable
        failure (auth invalid, permanent disconnect).
        """
        ...


# ─── Canonical venue registry ─────────────────────────────────────────

_venues: dict[str, type[Venue]] = {}


def register_venue(cls: type[Venue]) -> type[Venue]:
    """Decorator to register a concrete Venue implementation."""
    _venues[cls.name] = cls
    return cls


def get_venue(name: str) -> Venue:
    """Instantiate a registered venue by name.

    Usage: `kalshi = get_venue('kalshi')` — same across all callers.
    """
    cls = _venues.get(name)
    if cls is None:
        raise KeyError(f"unknown venue: {name}. Registered: {list(_venues.keys())}")
    return cls()


def list_venues() -> list[str]:
    return list(_venues.keys())
