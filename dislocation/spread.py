"""Cost-adjusted spread + edge calculator.

Given two implied probabilities for the SAME underlying event from two
venues, compute:

  raw_spread_pp     = |p_a - p_b|
  cost_pp           = round-trip cost on both legs, expressed in pp of
                      the underlying probability span
  edge_pp           = max(0, raw_spread_pp - cost_pp)
  expected_pnl_usd  = edge_pp/100 × position_usd × confidence
  hold_cost_usd     = funding × position × days_to_settle

A trade is "tradable" when edge_pp ≥ MIN_EDGE_PP (per config) AND
expected_pnl_usd > hold_cost_usd by a comfortable margin.

PHYSICS:
  Convergence trade payoff: long the cheap side, short the expensive side
  (in probability terms). At settlement, both legs resolve to the same
  outcome (0 or 1), so payoff = |p_b - p_a| − costs, regardless of which
  outcome occurs. THIS IS THE KEY INSIGHT — convergence is realized
  pathwise at settlement, not in expectation.

  Caveat: if the two markets settle on subtly different specs, you eat
  basis risk. The EventPair.resolver function must guarantee identical
  settlement outcomes. Audit per-domain in scanners.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config import (
    ANNUAL_FUNDING_PCT,
    CME_FUTURES_FEE_USD,
    EQUITY_FEE_BPS,
    KALSHI_FEE_PCT,
    KALSHI_TICK_SLIPPAGE,
    MIN_EDGE_PP,
    OPTIONS_FEE_USD,
    PM_FEE_PCT,
)
from .event_universe import Venue


def round_trip_cost_pp(venue: Venue, prob: float, position_usd: float) -> float:
    """Round-trip cost expressed in probability points of the position.

    For Kalshi/PM: fee is a % of contract notional. Per pp of move this
    is roughly fee_pct/prob_at_entry × 100 (rough; assumes you trade YES).

    For futures: fee is per-contract; convert to pp via contract_notional.
    For options: same as futures (per-contract fee).
    For equities: bps slippage.

    Approximation good enough for filtering. Refine per-venue in scanners
    when calibration data accumulates.
    """
    if position_usd <= 0:
        return 0.0

    if venue == Venue.KALSHI:
        # Kalshi official: fee = ⌈0.07 × C × P × (1-P)⌉ cents per side.
        # Per $1 of position: contracts ≈ 1/P, so fee/$1 = 0.07 × (1-P).
        # Round-trip = 2 × fee/$1 × position. Plus 1-tick slippage each side.
        fee_pp = 2 * KALSHI_FEE_PCT * (1.0 - max(prob, 0.01)) * 100.0
        slip_pp = 2 * KALSHI_TICK_SLIPPAGE * 100.0
        return fee_pp + slip_pp

    if venue == Venue.POLYMARKET:
        fee_usd = 2 * (PM_FEE_PCT * position_usd)
        return fee_usd / position_usd * 100.0

    if venue == Venue.CME_FUTURES:
        # ZQ contract notional ≈ $4.17/bp = $4170 per 1.00 price unit.
        # For our purposes: cost per pp of underlying probability is small
        # at typical convergence-trade sizes ($25-$500). Approximate as
        # 2 × fee × pp_per_dollar.
        contracts_implied = max(1, position_usd / 4170)
        fee_usd = 2 * CME_FUTURES_FEE_USD * contracts_implied
        return fee_usd / position_usd * 100.0

    if venue == Venue.CME_OPTIONS or venue == Venue.EQUITY_OPTS:
        contracts_implied = max(1, position_usd / 100)  # rough $100/contract
        fee_usd = 2 * OPTIONS_FEE_USD * contracts_implied
        return fee_usd / position_usd * 100.0

    if venue == Venue.EQUITIES:
        return 2 * EQUITY_FEE_BPS / 100.0  # bps → pp

    if venue == Venue.SPORTSBOOK:
        # Sportsbook vig is already removed in pricing/sportsbook.py.
        # Add a 1pp safety buffer for line movement.
        return 1.0

    if venue == Venue.ILS_SPREAD:
        # Off-market estimate, no real execution cost; signal-only domain.
        return 0.0

    return 1.0  # unknown venue: conservative 1pp.


def hold_cost_usd(position_usd: float, days_to_settle: float) -> float:
    """Funding cost on capital tied up in convergence trade."""
    return position_usd * (ANNUAL_FUNDING_PCT * days_to_settle / 365.0)


@dataclass
class SpreadAnalysis:
    pair_id:           str
    p_a:               float
    p_b:               float
    venue_a:           Venue
    venue_b:           Venue
    days_to_settle:    float
    position_usd:      float
    raw_spread_pp:     float
    cost_pp:           float
    edge_pp:            float
    expected_pnl_usd:   float
    hold_cost_usd:      float
    net_pnl_usd:        float    # expected_pnl − hold_cost
    direction:          str      # "long_a_short_b" or "long_b_short_a"
    tradable:           bool

    def explain(self) -> dict:
        return {
            "pair_id":          self.pair_id,
            "p_a":              round(self.p_a, 4),
            "p_b":              round(self.p_b, 4),
            "raw_spread_pp":    round(self.raw_spread_pp, 2),
            "cost_pp":          round(self.cost_pp, 2),
            "edge_pp":          round(self.edge_pp, 2),
            "days":             round(self.days_to_settle, 1),
            "position_$":       round(self.position_usd, 2),
            "expected_pnl_$":   round(self.expected_pnl_usd, 2),
            "hold_cost_$":      round(self.hold_cost_usd, 2),
            "net_pnl_$":        round(self.net_pnl_usd, 2),
            "direction":        self.direction,
            "tradable":         self.tradable,
        }


def analyze_spread(
    *,
    pair_id:        str,
    p_a:            float,
    p_b:            float,
    venue_a:        Venue,
    venue_b:        Venue,
    days_to_settle: float,
    position_usd:   float,
    confidence:     float = 1.0,
    min_edge_pp:    Optional[float] = None,
) -> SpreadAnalysis:
    """Compute cost-adjusted edge for a paired-market convergence trade.

    confidence ∈ [0, 1] — operator's prior on whether the two venues are
    truly pricing the same event (basis-risk discount). Default 1.0 for
    spec-matched pairs; 0.7-0.9 for soft-matched (e.g. weather across
    nearby cities).
    """
    threshold = MIN_EDGE_PP if min_edge_pp is None else min_edge_pp

    raw_spread_pp = abs(p_a - p_b) * 100.0
    cost_pp = (
        round_trip_cost_pp(venue_a, p_a, position_usd / 2.0)
        + round_trip_cost_pp(venue_b, p_b, position_usd / 2.0)
    )
    edge_pp = max(0.0, raw_spread_pp - cost_pp)

    expected_pnl_usd = (edge_pp / 100.0) * position_usd * max(0.0, min(1.0, confidence))
    hold = hold_cost_usd(position_usd, days_to_settle)
    net = expected_pnl_usd - hold

    direction = "long_a_short_b" if p_a < p_b else "long_b_short_a"
    tradable = (edge_pp >= threshold) and (net > 0)

    return SpreadAnalysis(
        pair_id=pair_id,
        p_a=p_a,
        p_b=p_b,
        venue_a=venue_a,
        venue_b=venue_b,
        days_to_settle=days_to_settle,
        position_usd=position_usd,
        raw_spread_pp=raw_spread_pp,
        cost_pp=cost_pp,
        edge_pp=edge_pp,
        expected_pnl_usd=expected_pnl_usd,
        hold_cost_usd=hold,
        net_pnl_usd=net,
        direction=direction,
        tradable=tradable,
    )


# ── Self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Scenario: Kalshi prices Fed cut at 60%, ZQ futures imply 75%.
    # Position $200, 30 days to settle.
    a = analyze_spread(
        pair_id="test-fed",
        p_a=0.60, p_b=0.75,
        venue_a=Venue.KALSHI, venue_b=Venue.CME_FUTURES,
        days_to_settle=30,
        position_usd=200,
    )
    print("scenario A (15pp raw spread):", a.explain())
    assert abs(a.raw_spread_pp - 15.0) < 0.001
    assert a.tradable, "15pp spread should be tradable"
    assert a.direction == "long_a_short_b"  # buy Kalshi (cheap), short ZQ

    # Scenario B: tiny spread, costs eat it.
    b = analyze_spread(
        pair_id="test-fed-tiny",
        p_a=0.62, p_b=0.63,
        venue_a=Venue.KALSHI, venue_b=Venue.CME_FUTURES,
        days_to_settle=30,
        position_usd=200,
    )
    print("scenario B (1pp raw spread):", b.explain())
    assert not b.tradable, "1pp spread should be killed by costs"

    # Scenario C: medium spread, long hold makes funding bite.
    c = analyze_spread(
        pair_id="test-long-hold",
        p_a=0.40, p_b=0.50,
        venue_a=Venue.KALSHI, venue_b=Venue.POLYMARKET,
        days_to_settle=180,
        position_usd=300,
    )
    print("scenario C (10pp, 180d hold):", c.explain())
    # Hold cost: 300 × 0.05 × 180/365 ≈ $7.4. Should still be net positive.

    print("spread self-test OK")
