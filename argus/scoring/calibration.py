"""Calibration curve fitting — Platt scaling skeleton.

Given a brain's raw probabilities and realized outcomes, fit a 1D
mapping f: raw_p -> calibrated_p so the calibration curve hugs the
diagonal. Two methods (Platt here, isotonic deferred):

    Platt scaling (sigmoid fit, parametric):
        calibrated_p = sigmoid(a * raw_logit + b)
        Robust to small N; good when miscalibration is roughly monotonic.

    Isotonic regression (non-parametric):
        Pool-Adjacent-Violators algorithm.
        Better fit, needs n >= 100ish to avoid overfitting.

Brain backtest reports BOTH raw-Brier and calibrated-Brier. If
calibration shifts Brier > 0.02 the model is poorly calibrated and
worth retraining.

Phase 1: Platt only with tiny SGD fit. Replace with sklearn-grade later.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class PlattScaler:
    """Sigmoid calibration: calibrated_p = sigmoid(a * raw_logit + b)."""
    a: float = 1.0
    b: float = 0.0
    n_fit: int = 0

    def transform(self, p: float) -> float:
        eps = 1e-9
        p = min(1 - eps, max(eps, p))
        logit = math.log(p / (1 - p))
        z = self.a * logit + self.b
        return 1.0 / (1.0 + math.exp(-z))

    def fit(
        self,
        raw_probs: list[float],
        outcomes:  list[int],
        lr:        float = 0.05,
        epochs:    int = 200,
    ) -> "PlattScaler":
        """Tiny SGD fit. Phase 1 sanity only — production should use
        scipy/sklearn for proper convergence guarantees."""
        eps = 1e-9
        for _ in range(epochs):
            grad_a = grad_b = 0.0
            for p, y in zip(raw_probs, outcomes):
                p = min(1 - eps, max(eps, p))
                logit = math.log(p / (1 - p))
                z = self.a * logit + self.b
                pred = 1.0 / (1.0 + math.exp(-z))
                err = pred - y
                grad_a += err * logit
                grad_b += err
            n = max(1, len(raw_probs))
            self.a -= lr * grad_a / n
            self.b -= lr * grad_b / n
        self.n_fit = len(raw_probs)
        return self


if __name__ == "__main__":
    # Synthetic: model is over-confident — at p=0.9 it's only right 70%
    raw = [0.9] * 10 + [0.1] * 10
    out = [1] * 7 + [0] * 3 + [1] * 1 + [0] * 9
    s = PlattScaler().fit(raw, out)
    c1 = s.transform(0.9)
    c2 = s.transform(0.1)
    print(f"calibrated 0.9 -> {c1:.3f}  (raw was over-confident, expect <0.9)")
    print(f"calibrated 0.1 -> {c2:.3f}")
    assert c1 < 0.9, "Platt should pull 0.9 down toward observed 0.7"
    print("OK calibration")
