"""Unified Maker Yield Equation — single source of truth for ranking.

Used by BOTH Kalshi and Polymarket scorers. Mirrors the official
liquidity incentive program math from both venues' docs.

PHYSICS:
    ExpectedDailyRebate = PoolPerDay
                          × OurShare
                          × QualifyProb
                          × TimeFactor
                          × Calibration
                        - AdverseCost

WHERE:
  PoolPerDay  : reward pool $ for the period / period_days
  OurShare    : our_size / (top_book_size + our_size)
                (at-best, ignoring depth behind us with DF^1+ which is
                 negligible vs our at-best score for tier-1 quoting)
  QualifyProb : sigmoid around target_size threshold; 0.5 at target,
                ~1.0 if 10% above, ~0.0 if 10% below.
                Replaces brutal binary gate that returned $0 on
                marginal qualification.
  TimeFactor  : exp(-days_to_settle / 90) — long markets attract
                competitors over time, share dilutes.
  Calibration : empirical haircut (theoretical → actual paid).
                Kalshi: ~0.25, PM: ~0.10 (verify with paid data).
  AdverseCost : 0.5%/day × worst_leg_$ × sqrt(days_held)
                Diffusion cost from informed flow.

YIELD-ON-CAPITAL (RANKING):
    yield_pct_daily = expected_daily_rebate / capital_at_risk
    capital_at_risk = our_size × max(midpoint, 1-midpoint)

USAGE (both venues):
    from yield_equation import MarketYield
    y = MarketYield(
        market_id="KXCORNW-...",
        pool_per_day=37.04,
        our_size=15,
        top_book_size=300,
        target_size=300,
        discount_factor=0.40,
        hours_to_settle=72,
        midpoint=0.45,
        calibration=0.25,
    )
    rank_by = y.expected_daily_rebate    # for ranking markets
    yield_pct = y.yield_pct_daily        # for capital-efficiency ranking
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class MarketYield:
    """All the inputs to compute expected rebate yield for one market."""
    market_id:        str
    pool_per_day:     float       # $ rewards / day for this market
    our_size:         int          # contracts we'd quote per side
    top_book_size:    int          # competitor contracts at best (avg of bid+ask sides)
    target_size:     int           # qualification threshold (aggregate raw size)
    discount_factor:  float       # DF^ticks_from_best — for at-best we score 1.0
    hours_to_settle:  float       # hours until market closes
    midpoint:         float       # current YES price midpoint [0,1]
    calibration:      float = 0.25  # empirical theoretical→actual ratio

    # Optional series-level boost (proven winner, niche bias, etc.)
    series_priority:  float = 1.0

    # Optional: empirically observed historical share for this market
    # (None = use theoretical formula; non-None = blend toward observed)
    observed_share:   float | None = None

    @property
    def our_share(self) -> float:
        """Fraction of qualifying snapshot score we capture."""
        # Theoretical: at-best, our_score = our_size, denominator = top + ours
        # Real depth behind us (DF^1, DF^2) can boost denominator slightly
        # but at_best dominates by 1/DF (~3-10x at typical DF=0.30-0.40)
        if self.observed_share is not None and self.observed_share > 0:
            # Blend 70% observed, 30% theoretical (smoothing)
            theoretical = self.our_size / max(1, self.top_book_size + self.our_size)
            return 0.7 * self.observed_share + 0.3 * theoretical
        return self.our_size / max(1, self.top_book_size + self.our_size)

    @property
    def qualify_prob(self) -> float:
        """Sigmoid probability we meet target_size threshold.
        0.5 at exactly target; ~1.0 if 10% above; ~0.0 if 10% below.
        Smoother than binary gate."""
        if self.target_size <= 0:
            return 1.0
        aggregate = self.top_book_size + self.our_size
        delta = aggregate - self.target_size
        slope = max(1.0, 0.10 * self.target_size)
        try:
            return 1.0 / (1.0 + math.exp(-delta / slope))
        except OverflowError:
            return 1.0 if delta > 0 else 0.0

    @property
    def time_factor(self) -> float:
        """Long markets dilute share. exp(-d/90):
        1d=0.99, 7d=0.93, 30d=0.72, 90d=0.37, 180d=0.14"""
        days = max(0.04, self.hours_to_settle / 24)
        return math.exp(-days / 90.0)

    @property
    def adverse_cost_per_day(self) -> float:
        """$ expected loss to informed flow per day held.
        50bps daily price drift × worst-leg position × sqrt(time).
        Diffusion variance grows linearly, std-dev grows sqrt."""
        days = max(0.04, self.hours_to_settle / 24)
        worst_leg_price = max(self.midpoint, 1.0 - self.midpoint)
        position_usd = self.our_size * worst_leg_price
        return 0.005 * math.sqrt(days) * position_usd

    @property
    def expected_daily_rebate(self) -> float:
        """Final $/day projection — what we ACTUALLY expect to earn."""
        gross = (self.pool_per_day
                 * self.our_share
                 * self.qualify_prob
                 * self.time_factor
                 * self.calibration
                 * self.series_priority)
        return max(0.0, gross - self.adverse_cost_per_day)

    @property
    def capital_at_risk(self) -> float:
        """$ at risk if both sides fully fill (worst case)."""
        worst_leg_price = max(self.midpoint, 1.0 - self.midpoint)
        return max(0.01, self.our_size * worst_leg_price)

    @property
    def yield_pct_daily(self) -> float:
        """Capital-efficiency ranking: $/day per $ deployed."""
        return self.expected_daily_rebate / self.capital_at_risk

    def explain(self) -> dict:
        """Show every component for transparency."""
        return {
            "market_id":        self.market_id,
            "pool_per_day":     round(self.pool_per_day, 2),
            "our_share":        round(self.our_share, 4),
            "qualify_prob":     round(self.qualify_prob, 3),
            "time_factor":      round(self.time_factor, 3),
            "adverse_cost":     round(self.adverse_cost_per_day, 4),
            "calibration":      self.calibration,
            "series_priority":  self.series_priority,
            "expected_$/d":     round(self.expected_daily_rebate, 4),
            "capital_$":        round(self.capital_at_risk, 2),
            "yield_%/d":        round(self.yield_pct_daily * 100, 2),
        }


# ── Convenience: rank markets by expected daily rebate ──
def rank_markets(yields: list[MarketYield]) -> list[MarketYield]:
    """Sort by expected daily rebate descending."""
    return sorted(yields, key=lambda y: -y.expected_daily_rebate)


def rank_by_yield_pct(yields: list[MarketYield]) -> list[MarketYield]:
    """Sort by capital-efficiency descending (best $/day per $ deployed)."""
    return sorted(yields, key=lambda y: -y.yield_pct_daily)
