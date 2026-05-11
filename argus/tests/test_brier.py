"""Tests for argus.scoring.brier."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus.scoring.brier import (
    brier_score,
    reliability_bins,
    naive_baseline_brier,
    brier_skill_score,
)


def test_brier_perfect():
    r = brier_score([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0])
    assert r.brier == 0.0
    assert r.skill == 1.0


def test_brier_coin_flip():
    r = brier_score([0.5] * 10, [1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
    assert abs(r.brier - 0.25) < 1e-9


def test_brier_handcalc():
    # Both predictions 0.9, both correct: SE = 0.01 each, brier = 0.01
    r = brier_score([0.9, 0.9], [1, 1])
    assert abs(r.brier - 0.01) < 1e-9


def test_brier_skill_negative_when_worse_than_base():
    # Always predict 0.0 but base rate = 0.5
    # brier = ((0-1)² × 5 + (0-0)² × 5) / 10 = 0.5
    # base_brier = 0.5 × 0.5 = 0.25
    # skill = 1 - 0.5 / 0.25 = -1.0
    r = brier_score([0.0] * 10, [1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
    assert abs(r.brier - 0.5) < 1e-9
    assert r.skill < 0


def test_brier_length_mismatch_raises():
    try:
        brier_score([0.5], [1, 0])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_reliability_bins_shape():
    b = reliability_bins([0.05, 0.15, 0.85, 0.95], [0, 0, 1, 1], n_bins=10)
    assert len(b) == 10
    nonempty = [x for x in b if x["n"] > 0]
    assert len(nonempty) == 4


def test_naive_baseline_brier_class_imbalance():
    # 5% YES base rate → naive_brier = 0.05 * 0.95 = 0.0475
    outs = [1] * 5 + [0] * 95
    nb = naive_baseline_brier(outs)
    assert abs(nb - 0.0475) < 1e-9, nb


def test_brier_skill_score_catches_naive_passing_raw_gate():
    # Class imbalance: 5% YES. Model "predicts 0.05 for everyone" → strictly naive.
    outs = [1] * 5 + [0] * 95
    naive_pred = [0.05] * 100
    r = brier_score(naive_pred, outs)
    bss = brier_skill_score(r.brier, outs)
    # Raw Brier passes the OLD 0.20 gate hilariously easily…
    assert r.brier < 0.10, f"naive brier={r.brier} — gate trap exposed"
    # …but BSS correctly registers ZERO improvement.
    assert abs(bss) < 0.01, f"BSS should be ~0 for naive predictor, got {bss}"


def test_brier_skill_score_positive_when_model_beats_naive():
    # Same class imbalance; model perfectly identifies the 5 YES.
    outs = [1] * 5 + [0] * 95
    perfect = [1.0] * 5 + [0.0] * 95
    r = brier_score(perfect, outs)
    bss = brier_skill_score(r.brier, outs)
    assert bss == 1.0, f"perfect predictor BSS should be 1.0, got {bss}"


if __name__ == "__main__":
    test_brier_perfect()
    test_brier_coin_flip()
    test_brier_handcalc()
    test_brier_skill_negative_when_worse_than_base()
    test_brier_length_mismatch_raises()
    test_reliability_bins_shape()
    test_naive_baseline_brier_class_imbalance()
    test_brier_skill_score_catches_naive_passing_raw_gate()
    test_brier_skill_score_positive_when_model_beats_naive()
    print("OK test_brier — 9/9 passed")
