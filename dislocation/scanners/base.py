"""Base class for dislocation scanners — one per domain.

Lifecycle per scan tick:
    pairs   = scanner.load_pairs()
    for pair in pairs:
        q_a, q_b = scanner.fetch_quotes(pair)
        if q_a is None or q_b is None:
            continue
        confidence = scanner.score_basis_risk(pair)
        spread     = analyze_spread(pair, q_a, q_b, confidence)
        decision   = size_trade(spread, bankroll, deployed, ...)
        scanner.emit(pair, spread, decision)

Subclasses override load_pairs() + fetch_quotes() + (optionally)
score_basis_risk(). Everything else is shared.
"""
from __future__ import annotations

import abc
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from ..config import MIN_DAYS_TO_SETTLE, MAX_DAYS_TO_SETTLE
from ..event_universe import Domain, EventPair, VenueQuote
from ..sizer import SizingDecision, size_trade
from ..spread import SpreadAnalysis, analyze_spread

_log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """A scanner's output for one pair on one tick."""
    pair:       EventPair
    spread:     SpreadAnalysis
    decision:   SizingDecision
    timestamp:  dt.datetime

    @property
    def actionable(self) -> bool:
        return self.spread.tradable and (not self.decision.rejected)

    def explain(self) -> dict:
        return {
            "pair_id":    self.pair.pair_id,
            "domain":     self.pair.domain.value,
            "desc":       self.pair.description,
            "spread":     self.spread.explain(),
            "decision":   self.decision.explain(),
            "ts":         self.timestamp.isoformat(),
            "actionable": self.actionable,
        }


class DislocationScanner(abc.ABC):
    """One-domain scanner. Subclass per niche."""

    domain: Domain  # subclass MUST set this

    # Per-domain basis-risk prior (probability the two venues actually price
    # the same event). Higher = wider Kelly window. Override per subclass.
    # Default 0.85 = ~15% chance our spec mapping is wrong / settles diverge.
    win_prob_prior: float = 0.85

    def __init__(self, *, bankroll: float, deployed: float = 0.0) -> None:
        self.bankroll = bankroll
        self.deployed = deployed

    @abc.abstractmethod
    def load_pairs(self) -> list[EventPair]:
        """Return the EventPairs this scanner is responsible for.

        Implementations may pull from a static config, a SQLite table, or
        an LLM-driven universe-builder pass — that's the only place LLMs
        legitimately help here.
        """
        ...

    @abc.abstractmethod
    def fetch_quotes(
        self, pair: EventPair
    ) -> tuple[Optional[VenueQuote], Optional[VenueQuote]]:
        """Fetch live (or last-known) quotes from venue_a and venue_b."""
        ...

    def score_basis_risk(self, pair: EventPair) -> float:
        """Confidence that the two markets resolve identically.

        Override per-domain. Default 1.0 = perfect spec match.
        For weather (NYC vs CHI proxy) try 0.7; for bio (FDA vs vol skew)
        0.5 is more honest. The lower this is, the harder it must work
        to clear the edge threshold.
        """
        return 1.0

    def position_usd_for(self, pair: EventPair) -> float:
        """Trial position size used for cost calculation. Sizer will adjust.

        Default: 5% of bankroll. Subclasses may want larger trial sizes for
        domains with low fees-per-pp (futures) and smaller for high-fee
        domains (PM 2% × 2 sides).
        """
        return self.bankroll * 0.05

    def scan(self, *, now: Optional[dt.datetime] = None) -> list[Candidate]:
        now = now or dt.datetime.utcnow()
        out: list[Candidate] = []
        for pair in self.load_pairs():
            d = pair.days_to_settle(now)
            if d < MIN_DAYS_TO_SETTLE or d > MAX_DAYS_TO_SETTLE:
                continue
            q_a, q_b = self.fetch_quotes(pair)
            if q_a is None or q_b is None:
                _log.debug(f"{pair.pair_id}: missing quote, skipping")
                continue
            p_a = q_a.implied_prob
            p_b = q_b.implied_prob
            if p_a is None or p_b is None:
                continue
            confidence = self.score_basis_risk(pair)
            position = self.position_usd_for(pair)
            spread = analyze_spread(
                pair_id=pair.pair_id,
                p_a=p_a,
                p_b=p_b,
                venue_a=pair.venue_a,
                venue_b=pair.venue_b,
                days_to_settle=d,
                position_usd=position,
                confidence=confidence,
            )
            decision = size_trade(
                spread,
                bankroll=self.bankroll,
                deployed=self.deployed,
                win_prob_prior=self.win_prob_prior,
            )
            out.append(Candidate(
                pair=pair, spread=spread, decision=decision, timestamp=now,
            ))
        return out
