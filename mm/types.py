"""Shared value types for the market-making core.

Prices are integer cents. Sizes are contract counts (fractional fills are
allowed, matching Kalshi fixed-point counts). One process owns these objects;
venue I/O is applied by the single writer in ``mm.order_machine``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VenueName(str, Enum):
    KALSHI = "kalshi"
    PMUS = "pmus"
    FORECASTEX = "forecastex"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class OrderState(str, Enum):
    PENDING_NEW = "pending_new"
    RESTING = "resting"
    PARTIAL = "partial"
    PENDING_CANCEL = "pending_cancel"
    PENDING_AMEND = "pending_amend"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"          # sent, no authoritative ack yet


# Legal transitions. Anything else is a bug, not a venue surprise — surprises
# go through ``reconcile``, which is allowed to jump to the venue's state.
_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING_NEW: frozenset({
        OrderState.RESTING, OrderState.PARTIAL, OrderState.FILLED,
        OrderState.REJECTED, OrderState.UNKNOWN, OrderState.CANCELLED,
    }),
    OrderState.UNKNOWN: frozenset({
        OrderState.RESTING, OrderState.PARTIAL, OrderState.FILLED,
        OrderState.CANCELLED, OrderState.REJECTED,
    }),
    OrderState.RESTING: frozenset({
        OrderState.PARTIAL, OrderState.FILLED, OrderState.PENDING_CANCEL,
        OrderState.PENDING_AMEND, OrderState.CANCELLED, OrderState.UNKNOWN,
    }),
    OrderState.PARTIAL: frozenset({
        OrderState.PARTIAL, OrderState.FILLED, OrderState.PENDING_CANCEL,
        OrderState.PENDING_AMEND, OrderState.CANCELLED, OrderState.RESTING,
    }),
    OrderState.PENDING_AMEND: frozenset({
        OrderState.RESTING, OrderState.PARTIAL, OrderState.FILLED,
        OrderState.CANCELLED, OrderState.UNKNOWN,
    }),
    OrderState.PENDING_CANCEL: frozenset({
        OrderState.CANCELLED, OrderState.FILLED, OrderState.PARTIAL,
        OrderState.RESTING, OrderState.UNKNOWN,
    }),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


def can_transition(src: OrderState, dst: OrderState) -> bool:
    return dst in _TRANSITIONS.get(src, frozenset())


@dataclass
class ManagedOrder:
    """Local view of one venue order. The venue snapshot wins on reconcile."""
    client_order_id: str
    venue: VenueName
    market: str
    side: Side
    price_cents: int
    remaining: float
    state: OrderState = OrderState.PENDING_NEW
    order_id: str = ""
    filled: float = 0.0
    queue_preserved: bool = True
    paper: bool = True
    updated_ts: float = 0.0


@dataclass
class BookTop:
    """Best bids on both Kalshi-style ladders. YES ask = 100 - best NO bid."""
    market: str
    yes_bid_cents: Optional[int]
    no_bid_cents: Optional[int]
    yes_bid_size: float = 0.0
    no_bid_size: float = 0.0
    ts: float = 0.0

    @property
    def yes_mid_cents(self) -> Optional[float]:
        if self.yes_bid_cents is None or self.no_bid_cents is None:
            return None
        ask = 100 - self.no_bid_cents
        if ask < self.yes_bid_cents:
            return None
        return (self.yes_bid_cents + ask) / 2.0


@dataclass
class QuoteTarget:
    market: str
    venue: VenueName
    yes_bid_cents: Optional[int]
    no_bid_cents: Optional[int]
    yes_size: float
    no_size: float
    reason: str = ""
    fade_topup: bool = False


@dataclass
class FillRecord:
    trade_id: str
    order_id: str
    market: str
    venue: VenueName
    side: Side
    count: float
    price_cents: int
    is_maker: bool
    ts: float
    fee_usd: str = "0"          # Decimal as string so logs stay exact
    rebate_usd: str = "0"


@dataclass
class VenueOrderView:
    """What the venue says an order looks like, after parsing."""
    order_id: str
    client_order_id: str
    market: str
    side: Side
    price_cents: int
    remaining: float
    status: str                 # resting | filled | cancelled | ...


@dataclass
class ReconcileReport:
    adopted: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
