"""Unified Maker Yield Equation — single source of truth (cross_venue/).

Used by BOTH Kalshi and Polymarket scorers. Mirrors the official
liquidity incentive program math from both venues' docs.

⚠️  THIS IS THE CANONICAL COPY. The shims at:
    - engine/yield_equation.py            (Kalshi side)
    - polymarket/engine/yield_equation.py (PM side)
both re-export from here to avoid divergence.

PHYSICS:
    ExpectedDailyRebate = PoolPerDay
                          × OurShare
                          × QualifyProb
                          × TimeFactor
                          × Calibration       (REQUIRED — no default)
                        - AdverseCost

CALIBRATION (empirical):
    Kalshi: ~0.25 (theoretical / actual paid)
    PM:     ~0.10 (theoretical / actual paid)
    Caller MUST pass venue-specific value. No default = no silent divergence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# Convenience constants for callers — venue-specific calibration
KALSHI_CALIB = 0.25
PM_CALIB     = 0.10


@dataclass
class MarketYield:
    """All inputs to compute expected rebate yield for one market.

    NOTE: `calibration` has NO DEFAULT. Caller MUST pass venue-specific
    value (KALSHI_CALIB or PM_CALIB) to prevent the cross-venue projection bug.
    """
    market_id:        str
    pool_per_day:     float
    our_size:         int
    top_book_size:    int
    target_size:      int
    discount_factor:  float
    hours_to_settle:  float
    midpoint:         float
    calibration:      float       # REQUIRED
    series_priority:  float = 1.0
    observed_share:   float | None = None

    @property
    def our_share(self) -> float:
        if self.observed_share is not None and self.observed_share > 0:
            theoretical = self.our_size / max(1, self.top_book_size + self.our_size)
            return 0.7 * self.observed_share + 0.3 * theoretical
        return self.our_size / max(1, self.top_book_size + self.our_size)

    @property
    def qualify_prob(self) -> float:
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
        days = max(0.04, self.hours_to_settle / 24)
        return math.exp(-days / 90.0)

    @property
    def adverse_cost_per_day(self) -> float:
        days = max(0.04, self.hours_to_settle / 24)
        worst_leg_price = max(self.midpoint, 1.0 - self.midpoint)
        position_usd = self.our_size * worst_leg_price
        return 0.005 * math.sqrt(days) * position_usd

    @property
    def expected_daily_rebate(self) -> float:
        gross = (self.pool_per_day
                 * self.our_share
                 * self.qualify_prob
                 * self.time_factor
                 * self.calibration
                 * self.series_priority)
        return max(0.0, gross - self.adverse_cost_per_day)

    @property
    def capital_at_risk(self) -> float:
        worst_leg_price = max(self.midpoint, 1.0 - self.midpoint)
        return max(0.01, self.our_size * worst_leg_price)

    @property
    def yield_pct_daily(self) -> float:
        return self.expected_daily_rebate / self.capital_at_risk

    def explain(self) -> dict:
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


def rank_markets(yields: list[MarketYield]) -> list[MarketYield]:
    return sorted(yields, key=lambda y: -y.expected_daily_rebate)


def rank_by_yield_pct(yields: list[MarketYield]) -> list[MarketYield]:
    return sorted(yields, key=lambda y: -y.yield_pct_daily)
