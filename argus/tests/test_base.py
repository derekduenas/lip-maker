"""Tests for argus.brains.base."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus.brains.base import DomainBrain, Prediction, confidence_from_extremes


def test_domain_brain_is_abstract():
    try:
        DomainBrain()  # type: ignore[abstract]
    except TypeError:
        return
    raise AssertionError("DomainBrain() should fail without abstract methods")


def test_concrete_brain_works():
    class Stub(DomainBrain):
        brain_id = "stub"
        def can_evaluate(self, t): return t == "X"
        def predict(self, m): return None
    s = Stub()
    assert s.can_evaluate("X")
    assert not s.can_evaluate("Y")


def test_prediction_validates_range():
    Prediction(p_yes=0.0, confidence=0.0, key_features={},
               brain_id="t", market_ticker="X")
    Prediction(p_yes=1.0, confidence=1.0, key_features={},
               brain_id="t", market_ticker="X")
    for bad in (-0.1, 1.1):
        try:
            Prediction(p_yes=bad, confidence=0.5, key_features={},
                       brain_id="t", market_ticker="X")
        except ValueError:
            pass
        else:
            raise AssertionError(f"p_yes={bad} should have raised")


def test_edge_vs_market_signed():
    class Stub(DomainBrain):
        brain_id = "stub"
        def can_evaluate(self, t): return True
        def predict(self, m): return None
    s = Stub()
    p = Prediction(p_yes=0.7, confidence=0.5, key_features={},
                   brain_id="stub", market_ticker="X")
    assert abs(s.edge_vs_market(p, 0.5) - 0.2) < 1e-9
    assert abs(s.edge_vs_market(p, 0.9) + 0.2) < 1e-9


def test_confidence_from_extremes():
    assert confidence_from_extremes(0.5) == 0.0
    assert confidence_from_extremes(0.0) == 1.0
    assert confidence_from_extremes(1.0) == 1.0
    # confidence_from_extremes(0.05) = 1 - 4*0.05*0.95 = 0.81
    assert 0.80 < confidence_from_extremes(0.05) <= 1.0


if __name__ == "__main__":
    test_domain_brain_is_abstract()
    test_concrete_brain_works()
    test_prediction_validates_range()
    test_edge_vs_market_signed()
    test_confidence_from_extremes()
    print("OK test_base — 5/5 passed")
