"""Brier score — mean squared error of predicted probabilities.

Definition (binary case):
    Brier = (1/N) * sum( (p_pred_i - outcome_i)^2 )

Where outcome_i in {0, 1}. Range [0, 1]:
    0.00  perfect prediction
    0.25  always-50% (coin flip)
    0.50  worst (always confidently wrong)

Skill score:
    skill = 1 - Brier / Brier_baseline
    where Brier_baseline = base_rate * (1 - base_rate)
    skill > 0  → beats the always-base-rate predictor
    skill = 0  → no better than base rate
    skill < 0  → worse than base rate
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BrierResult:
    n:         int
    brier:     float        # mean squared error
    base_rate: float        # mean(outcome) — null model reference
    skill:     float        # 1 - brier / brier_base; > 0 = beats baseline

    def explain(self) -> dict:
        return {
            "n":         self.n,
            "brier":     round(self.brier, 4),
            "base_rate": round(self.base_rate, 4),
            "skill":     round(self.skill, 4),
        }


def naive_baseline_brier(outcomes: list[int]) -> float:
    """Brier of the naive 'predict base_rate for every market' model.

    base_rate = mean(outcomes); naive_brier = base_rate * (1 - base_rate).
    This is the floor any honest model must beat. Critical when class
    imbalance is heavy (e.g. 5-10% YES base rate → naive brier ~0.05;
    a raw "Brier <= 0.20" gate passes models strictly worse than naive).
    """
    if not outcomes:
        return 0.0
    base_rate = sum(outcomes) / len(outcomes)
    return base_rate * (1.0 - base_rate)


def brier_skill_score(model_brier: float, outcomes: list[int]) -> float:
    """Brier Skill Score: 1 - (model_brier / naive_brier).

    > 0  model beats predicting base rate for every case
    = 0  no improvement
    < 0  worse than naive
    """
    nb = naive_baseline_brier(outcomes)
    if nb <= 0:
        return 0.0
    return 1.0 - (model_brier / nb)


def brier_score(pred_probs: list[float], outcomes: list[int]) -> BrierResult:
    """Compute Brier score + skill-vs-baseline."""
    if len(pred_probs) != len(outcomes):
        raise ValueError(
            f"length mismatch: preds={len(pred_probs)} outcomes={len(outcomes)}"
        )
    n = len(pred_probs)
    if n == 0:
        return BrierResult(n=0, brier=0.0, base_rate=0.0, skill=0.0)
    for o in outcomes:
        if o not in (0, 1):
            raise ValueError(f"outcome must be 0 or 1, got {o}")
    base_rate = sum(outcomes) / n
    brier = sum((p - o) ** 2 for p, o in zip(pred_probs, outcomes)) / n
    brier_base = base_rate * (1 - base_rate) if 0 < base_rate < 1 else 0.25
    skill = 1.0 - (brier / brier_base) if brier_base > 0 else 0.0
    return BrierResult(n=n, brier=brier, base_rate=base_rate, skill=skill)


def reliability_bins(
    pred_probs: list[float],
    outcomes:   list[int],
    n_bins:     int = 10,
) -> list[dict]:
    """Bin predictions into [0, 1/n_bins, ..., 1.0] and compare
    predicted_avg vs realized_freq per bin.
    """
    if len(pred_probs) != len(outcomes):
        raise ValueError("length mismatch")
    bins: list[dict] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        # last bin closed on right
        in_bin = [
            (p, o) for p, o in zip(pred_probs, outcomes)
            if (lo <= p < hi) or (i == n_bins - 1 and p == hi)
        ]
        n = len(in_bin)
        if n == 0:
            bins.append({
                "bin_lo": lo, "bin_hi": hi, "n": 0,
                "predicted_avg": None, "realized_freq": None,
            })
            continue
        pred_avg  = sum(p for p, _ in in_bin) / n
        real_freq = sum(o for _, o in in_bin) / n
        bins.append({
            "bin_lo": lo, "bin_hi": hi, "n": n,
            "predicted_avg": round(pred_avg, 4),
            "realized_freq": round(real_freq, 4),
        })
    return bins


if __name__ == "__main__":
    # SE = (0.9-1)^2 * 2 + (0.1-0)^2 * 2 = 0.04; Brier = 0.04/4 = 0.01
    r = brier_score([0.9, 0.9, 0.1, 0.1], [1, 1, 0, 0])
    print(f"perfect-ish: brier={r.brier:.4f}  expected ~0.0100")
    assert abs(r.brier - 0.01) < 1e-6, r.brier

    r = brier_score([0.5] * 10, [1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
    print(f"coin-flip: brier={r.brier:.4f}  expected 0.25")
    assert abs(r.brier - 0.25) < 1e-6

    bins = reliability_bins([0.05, 0.15, 0.85, 0.95], [0, 0, 1, 1], n_bins=10)
    nonempty = [b for b in bins if b["n"] > 0]
    print(f"reliability bins (non-empty): {len(nonempty)}")
    for b in nonempty:
        print(f"  [{b['bin_lo']:.1f}, {b['bin_hi']:.1f}): "
              f"n={b['n']} pred={b['predicted_avg']} real={b['realized_freq']}")
    print("OK brier")
