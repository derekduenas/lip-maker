"""Kelly-bounded sizer for ARGUS predictions.

Adapted from dislocation/sizer.py with two changes:
  1. Input is a Prediction (own probability + confidence) instead of a
     SpreadAnalysis (cross-venue spread). Math identical — Kelly only
     cares about win prob and odds.
  2. Adds a confidence multiplier on top of fractional-Kelly: low-
     confidence predictions get further size shrinkage.

Kelly formula (binary, fair odds):
    f* = (p * b - q) / b
    where p = win prob, q = 1 - p, b = odds received per unit risked.

For Kalshi binary at price market_p, buying YES:
    win pays  (1 - market_p) / market_p   if it settles YES
    lose costs market_p                   if it settles NO
    -> b = (1 - market_p) / market_p

If our prediction p_yes < market_p, we take NO instead — symmetric Kelly
on (1 - p_yes) at price (1 - market_p).

Final cap stack:
    final_f = (KELLY_FRACTION * confidence * f*) capped by:
        per-trade $ (MAX_TRADE_USD AND % of bankroll),
        total deployed cap (% of bankroll across all open positions),
        MAE buffer divisor (size assuming 2x expected adverse excursion),
        MIN_TRADE_USD floor (or reject).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from argus.brains.base import Prediction
from argus.config import (
    KELLY_FRACTION,
    MAE_BUFFER_MULT,
    MAX_DEPLOYED_PCT,
    MAX_TRADE_PCT_OF_BANKROLL,
    MAX_TRADE_USD,
    MIN_TRADE_USD,
)

_log = logging.getLogger(__name__)


@dataclass
class SizingDecision:
    market_ticker:        str
    side:                 str               # "yes" or "no"
    raw_kelly_usd:        float
    fractional_kelly_usd: float
    bankroll_capped_usd:  float
    final_usd:            float
    rationale:            list[str] = field(default_factory=list)
    rejected:             bool = False

    def explain(self) -> dict:
        return {
            "market":       self.market_ticker,
            "side":         self.side,
            "raw_kelly_$":  round(self.raw_kelly_usd, 2),
            "frac_kelly_$": round(self.fractional_kelly_usd, 2),
            "capped_$":     round(self.bankroll_capped_usd, 2),
            "final_$":      round(self.final_usd, 2),
            "rationale":    self.rationale,
            "rejected":     self.rejected,
        }


def kelly_fraction(p_win: float, market_p: float) -> float:
    """Kelly fraction for buying YES at market_p with own win prob p_win.
    Returns f* in [0, 1]; 0 if no edge."""
    if market_p <= 0 or market_p >= 1:
        return 0.0
    if p_win <= market_p:
        return 0.0
    b = (1.0 - market_p) / market_p
    q = 1.0 - p_win
    f = (p_win * b - q) / b
    return max(0.0, min(1.0, f))


def size_prediction(
    prediction: Prediction,
    market_p:   float,
    *,
    bankroll:        float,
    deployed_usd:    float = 0.0,
) -> SizingDecision:
    """Size a bet on `prediction.market_ticker` at observed `market_p`."""
    rationale: list[str] = []

    # Pick the side our prediction favors
    if prediction.p_yes >= market_p:
        side  = "yes"
        p_win = prediction.p_yes
        price = market_p
    else:
        side  = "no"
        p_win = 1.0 - prediction.p_yes
        price = 1.0 - market_p

    f_star    = kelly_fraction(p_win, price)
    raw_kelly = bankroll * f_star
    rationale.append(f"raw Kelly f*={f_star:.4f} -> ${raw_kelly:.2f}")

    if f_star <= 0:
        return SizingDecision(
            market_ticker=prediction.market_ticker, side=side,
            raw_kelly_usd=0, fractional_kelly_usd=0,
            bankroll_capped_usd=0, final_usd=0,
            rationale=rationale + ["no edge -> reject"], rejected=True,
        )

    frac = raw_kelly * KELLY_FRACTION * prediction.confidence
    rationale.append(
        f"x {KELLY_FRACTION} x conf {prediction.confidence:.2f} -> ${frac:.2f}"
    )

    # Per-trade ceiling
    per_trade_cap = min(MAX_TRADE_USD, bankroll * MAX_TRADE_PCT_OF_BANKROLL)
    capped = min(frac, per_trade_cap)
    if capped < frac:
        rationale.append(f"per-trade cap ${per_trade_cap:.2f}")

    # Total deployed cap (headroom)
    headroom = max(0.0, bankroll * MAX_DEPLOYED_PCT - deployed_usd)
    if capped > headroom:
        capped = headroom
        rationale.append(f"deployed-cap headroom ${headroom:.2f}")

    # MAE buffer
    capped /= MAE_BUFFER_MULT
    rationale.append(f"MAE buffer /{MAE_BUFFER_MULT} -> ${capped:.2f}")

    if capped < MIN_TRADE_USD:
        return SizingDecision(
            market_ticker=prediction.market_ticker, side=side,
            raw_kelly_usd=raw_kelly, fractional_kelly_usd=frac,
            bankroll_capped_usd=capped, final_usd=0,
            rationale=rationale + [f"< MIN_TRADE_USD ${MIN_TRADE_USD} -> reject"],
            rejected=True,
        )

    return SizingDecision(
        market_ticker=prediction.market_ticker, side=side,
        raw_kelly_usd=raw_kelly, fractional_kelly_usd=frac,
        bankroll_capped_usd=capped, final_usd=round(capped, 2),
        rationale=rationale,
    )


if __name__ == "__main__":
    pred = Prediction(
        p_yes=0.70, confidence=0.8, key_features={},
        brain_id="test", market_ticker="X",
    )
    d = size_prediction(pred, market_p=0.50, bankroll=1000)
    print(d.explain())
    assert d.final_usd > 0 and not d.rejected, d

    pred2 = Prediction(
        p_yes=0.50, confidence=0.8, key_features={},
        brain_id="test", market_ticker="Y",
    )
    d2 = size_prediction(pred2, market_p=0.50, bankroll=1000)
    print(d2.explain())
    assert d2.rejected, d2
    print("OK sizer")
