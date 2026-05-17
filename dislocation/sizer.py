"""Kelly-bounded convergence-trade sizer.

Sizes positions based on:
  1. Edge magnitude (bigger edge → bigger size, capped by Kelly).
  2. Adverse-excursion buffer (size assuming 2x MAE before convergence).
  3. Bankroll caps (per-trade %, total deployed %).
  4. Correlation guardrails (don't pile on highly-correlated pairs).

Kelly formula for binary outcomes with edge:
  f* = p × b - q
       ─────────
            b
  where p = our probability of winning (= 0.5 + edge_pp/100 conservatively),
        q = 1 - p,
        b = odds received on the wager (= 1.0 for a fair convergence bet).

Convergence trades aren't fair coin flips — they realize at settlement
with high probability of converging. But there's basis risk (~10% of
trades will diverge for spec mismatches we missed). We treat p = 0.85
as default and let the operator tune in config.

Quarter-Kelly (KELLY_FRACTION=0.25) is the default — full Kelly is too
volatile in practice and basis risk on convergence trades is not perfectly
estimated. Quarter-Kelly preserves geometric growth with much lower DD.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config import (
    KELLY_FRACTION,
    MAE_BUFFER_MULT,
    MAX_DEPLOYED_PCT,
    MAX_TRADE_PCT_OF_BANKROLL,
    MAX_TRADE_USD,
    MIN_TRADE_USD,
)
from .spread import SpreadAnalysis


@dataclass
class SizingDecision:
    pair_id:        str
    raw_kelly_usd:  float
    fractional_kelly_usd: float
    bankroll_capped_usd:  float
    final_usd:      float
    rationale:      list[str]    # human-readable trail of caps applied
    rejected:       bool

    def explain(self) -> dict:
        return {
            "pair_id":          self.pair_id,
            "raw_kelly_$":      round(self.raw_kelly_usd, 2),
            "frac_kelly_$":     round(self.fractional_kelly_usd, 2),
            "bankroll_cap_$":   round(self.bankroll_capped_usd, 2),
            "final_$":          round(self.final_usd, 2),
            "rationale":        self.rationale,
            "rejected":         self.rejected,
        }


def kelly_fraction(edge_pp: float, win_prob: float = 0.85) -> float:
    """Kelly fraction f* for a convergence trade.

    edge_pp:    raw spread minus costs (probability points).
    win_prob:   our prior that the convergence will be realized
                (default 0.85 = ~15% basis-risk haircut).

    Treat the trade as a binary bet where:
      - winning pays edge_pp/100 of position
      - losing costs the full position (worst case basis-risk blowup)
    Then f* = (p × b - q) / b, with b = edge_pp / 100.
    """
    if edge_pp <= 0 or win_prob <= 0 or win_prob >= 1:
        return 0.0
    b = max(0.005, edge_pp / 100.0)   # avoid div by zero, cap min odds
    q = 1.0 - win_prob
    f_star = (win_prob * b - q) / b
    return max(0.0, min(1.0, f_star))


def size_trade(
    spread:      SpreadAnalysis,
    bankroll:    float,
    *,
    deployed:    float = 0.0,
    correlated_exposure: float = 0.0,
    win_prob_prior: float = 0.85,
) -> SizingDecision:
    """Decide $ position size for one convergence-trade candidate.

    Flow:
      1. Reject outright if not tradable.
      2. Compute raw Kelly → fractional Kelly.
      3. Cap by per-trade %, total deployed %, correlation guardrail.
      4. Floor / ceiling clamps.
    """
    rationale: list[str] = []

    if not spread.tradable:
        return SizingDecision(
            pair_id=spread.pair_id,
            raw_kelly_usd=0.0,
            fractional_kelly_usd=0.0,
            bankroll_capped_usd=0.0,
            final_usd=0.0,
            rationale=["spread not tradable: edge below threshold or net pnl ≤ 0"],
            rejected=True,
        )

    f_star = kelly_fraction(spread.edge_pp, win_prob=win_prob_prior)
    raw_kelly_usd = bankroll * f_star
    rationale.append(f"raw Kelly f*={f_star:.4f} on bankroll {bankroll:.0f} = ${raw_kelly_usd:.0f}")

    fractional_kelly_usd = raw_kelly_usd * KELLY_FRACTION
    rationale.append(f"× quarter-Kelly ({KELLY_FRACTION}) = ${fractional_kelly_usd:.0f}")

    # Per-trade % cap.
    per_trade_cap = bankroll * MAX_TRADE_PCT_OF_BANKROLL
    capped = min(fractional_kelly_usd, per_trade_cap)
    if capped < fractional_kelly_usd:
        rationale.append(f"per-trade cap ${per_trade_cap:.0f} binding")

    # Total deployed cap.
    deployed_cap = bankroll * MAX_DEPLOYED_PCT - deployed
    if deployed_cap <= 0:
        rationale.append("deployed cap reached — reject")
        return SizingDecision(
            pair_id=spread.pair_id,
            raw_kelly_usd=raw_kelly_usd,
            fractional_kelly_usd=fractional_kelly_usd,
            bankroll_capped_usd=0.0,
            final_usd=0.0,
            rationale=rationale,
            rejected=True,
        )
    capped = min(capped, deployed_cap)

    # Correlation guardrail: if we already have $X in correlated trades,
    # halve the new size (rough — refine per-domain in scanners).
    if correlated_exposure > bankroll * 0.10:
        capped *= 0.5
        rationale.append(f"correlation discount (½x) due to ${correlated_exposure:.0f} correlated exposure")

    # Adverse-excursion buffer: shrink position so 2× MAE doesn't blow margin.
    mae_haircut = 1.0 / MAE_BUFFER_MULT
    capped *= mae_haircut
    rationale.append(f"× MAE buffer ({mae_haircut:.2f}) for adverse excursion")

    # Floor / ceiling.
    final = max(0.0, min(MAX_TRADE_USD, capped))
    if final < MIN_TRADE_USD:
        rationale.append(f"below MIN_TRADE_USD ${MIN_TRADE_USD} — reject")
        return SizingDecision(
            pair_id=spread.pair_id,
            raw_kelly_usd=raw_kelly_usd,
            fractional_kelly_usd=fractional_kelly_usd,
            bankroll_capped_usd=capped,
            final_usd=0.0,
            rationale=rationale,
            rejected=True,
        )

    rationale.append(f"final size ${final:.0f}")
    return SizingDecision(
        pair_id=spread.pair_id,
        raw_kelly_usd=raw_kelly_usd,
        fractional_kelly_usd=fractional_kelly_usd,
        bankroll_capped_usd=capped,
        final_usd=final,
        rationale=rationale,
        rejected=False,
    )


# ── Self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from .event_universe import Venue
    from .spread import analyze_spread

    # 20pp spread, $500 position, $1000 bankroll, 95% basis confidence
    # (macro/Fed-style spec match — costs amortize over larger position).
    sp = analyze_spread(
        pair_id="test",
        p_a=0.55, p_b=0.75,
        venue_a=Venue.KALSHI, venue_b=Venue.CME_FUTURES,
        days_to_settle=30,
        position_usd=500,
    )
    d = size_trade(sp, bankroll=1000, deployed=0, win_prob_prior=0.95)
    print("20pp spread / $1k bankroll / 0.95 prior:", d.explain())
    assert not d.rejected
    assert d.final_usd >= MIN_TRADE_USD
    assert d.final_usd <= 100  # 10% of $1k bankroll cap

    # Same spread but 0.85 prior (typical, conservative): may still trigger
    # at this edge but smaller. Let's test a 5pp edge case being rejected.
    sp_tight = analyze_spread(
        pair_id="test-tight",
        p_a=0.60, p_b=0.75,
        venue_a=Venue.KALSHI, venue_b=Venue.CME_FUTURES,
        days_to_settle=30,
        position_usd=200,
    )
    d_conservative = size_trade(sp_tight, bankroll=1000, deployed=0, win_prob_prior=0.85)
    print("5pp edge / 0.85 prior (conservative):", d_conservative.explain())
    assert d_conservative.rejected, "0.85 prior should reject sub-17pp edges"

    # Sub-threshold spread → reject.
    sp2 = analyze_spread(
        pair_id="test2",
        p_a=0.62, p_b=0.63,
        venue_a=Venue.KALSHI, venue_b=Venue.CME_FUTURES,
        days_to_settle=30,
        position_usd=200,
    )
    d2 = size_trade(sp2, bankroll=1000)
    print("1pp spread / $1k bankroll:", d2.explain())
    assert d2.rejected

    # Already deployed past cap → reject.
    d3 = size_trade(sp, bankroll=1000, deployed=600)
    print("deployed=$600/$500-cap:", d3.explain())
    assert d3.rejected

    # Kelly fraction sanity: edge_pp=10, win_prob=0.85 → b=0.10, q=0.15
    # f* = (0.85 × 0.10 - 0.15) / 0.10 = -0.65 → clipped to 0.
    # That's the "edge insufficient to overcome basis risk" signal.
    f_low = kelly_fraction(10.0, win_prob=0.85)
    print(f"kelly(10pp, p=0.85) = {f_low:.4f}")
    # With higher win_prob (e.g. 0.95) Kelly turns positive:
    f_hi = kelly_fraction(10.0, win_prob=0.95)
    print(f"kelly(10pp, p=0.95) = {f_hi:.4f}")
    assert f_hi > f_low

    print("sizer self-test OK")
