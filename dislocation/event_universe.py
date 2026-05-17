"""Event Universe — registry of paired markets across venues.

An EventPair links two markets at different venues that resolve on the SAME
underlying event. Both must agree by settlement; spread between their implied
probabilities is the convergence-trade edge.

Examples:
    "Fed cuts 25bp at next meeting" pairs:
        Kalshi:  KXFEDDECISION-26JUN25-CUT25  (binary YES/NO)
        CME:     ZQM26 (Fed funds futures, 30-day avg rate for June)

    "Hurricane hits Cat 3+" pairs:
        Kalshi:  KXHURRICANE-26-CAT3
        Cat-bond ILS spread (off-market estimate)

    "$TICKER beats EPS" pairs:
        Kalshi:  KXEARN-TICKER-Q2-BEAT
        Options: implied move from straddle ATM expiry-after-earnings

Each pair carries a settlement_resolver — a function that maps the venue-A
market outcome to the equivalent venue-B outcome at settlement, so the
convergence math is unambiguous.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Venue(Enum):
    KALSHI       = "kalshi"
    POLYMARKET   = "polymarket"
    CME_FUTURES  = "cme_futures"
    CME_OPTIONS  = "cme_options"
    EQUITIES     = "equities"
    EQUITY_OPTS  = "equity_options"
    ILS_SPREAD   = "ils_spread"
    SPORTSBOOK   = "sportsbook"


class Domain(Enum):
    """Each domain owns one Scanner subclass. Add domains as you build scanners."""
    MACRO_FED    = "macro_fed"          # Fed rate decisions vs FF futures
    MACRO_CPI    = "macro_cpi"          # CPI prints vs TIPS breakevens
    WEATHER      = "weather"            # Kalshi weather vs NWS / catastrophe spreads
    BIO_FDA      = "bio_fda"            # FDA approval markets vs biotech option vol
    EARNINGS     = "earnings"           # Earnings beat markets vs options straddle
    ELECTION     = "election"           # PM/Kalshi election vs sportsbook lines
    SPORTS       = "sports"             # Game outcome markets vs sportsbook
    MUSIC_CHART  = "music_chart"        # Spotify rank markets vs domain model


@dataclass
class VenueQuote:
    """Live quote for one side of a pair."""
    venue:        Venue
    market_id:    str
    yes_bid:      Optional[float] = None    # probability units, [0, 1]
    yes_ask:      Optional[float] = None
    mid:          Optional[float] = None
    size_at_best: Optional[int]   = None    # for liquidity gating
    timestamp:    Optional[dt.datetime] = None

    @property
    def implied_prob(self) -> Optional[float]:
        if self.mid is not None:
            return self.mid
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2.0
        return None


@dataclass
class EventPair:
    """One paired market across two venues, plus settlement resolver.

    pair_id  is your internal handle (used for trade attribution).
    domain   determines which Scanner owns this pair.
    settle_at is the canonical settlement timestamp; if the two venues
              settle at slightly different times, use the LATER one (you
              hold both legs until the slower side settles).
    """
    pair_id:        str
    domain:         Domain
    description:    str
    venue_a:        Venue
    market_a_id:    str
    venue_b:        Venue
    market_b_id:    str
    settle_at:      dt.datetime
    # resolver: given outcome on venue_a (1.0=YES, 0.0=NO), what's the
    # equivalent outcome on venue_b? Default = identity (same-spec pair).
    resolver:       Callable[[float], float] = field(
        default=lambda outcome_a: outcome_a
    )
    # Optional metadata for scanner-specific logic.
    meta:           dict = field(default_factory=dict)

    def days_to_settle(self, now: Optional[dt.datetime] = None) -> float:
        now = now or dt.datetime.utcnow()
        return max(0.0, (self.settle_at - now).total_seconds() / 86400.0)


class EventUniverse:
    """In-memory registry of EventPairs. Persistence handled separately."""

    def __init__(self) -> None:
        self._pairs: dict[str, EventPair] = {}

    def register(self, pair: EventPair) -> None:
        if pair.pair_id in self._pairs:
            raise ValueError(f"duplicate pair_id: {pair.pair_id}")
        self._pairs[pair.pair_id] = pair

    def by_domain(self, domain: Domain) -> list[EventPair]:
        return [p for p in self._pairs.values() if p.domain == domain]

    def all(self) -> list[EventPair]:
        return list(self._pairs.values())

    def get(self, pair_id: str) -> Optional[EventPair]:
        return self._pairs.get(pair_id)

    def __len__(self) -> int:
        return len(self._pairs)


# ── Self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    u = EventUniverse()
    u.register(EventPair(
        pair_id="test-fed-25bp-jun26",
        domain=Domain.MACRO_FED,
        description="Fed cuts 25bp at June 2026 FOMC",
        venue_a=Venue.KALSHI,
        market_a_id="KXFEDDECISION-26JUN-CUT25",
        venue_b=Venue.CME_FUTURES,
        market_b_id="ZQM26",
        settle_at=dt.datetime(2026, 6, 17, 18, 0, 0),
    ))
    assert len(u) == 1
    assert u.get("test-fed-25bp-jun26") is not None
    assert u.by_domain(Domain.MACRO_FED)[0].days_to_settle(
        dt.datetime(2026, 5, 9)
    ) > 38
    print("event_universe self-test OK")
