"""Tests for argus.execution.sizer."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus.brains.base import Prediction
from argus.execution.sizer import kelly_fraction, size_prediction


def test_kelly_no_edge_returns_zero():
    assert kelly_fraction(p_win=0.5, market_p=0.5) == 0.0
    assert kelly_fraction(p_win=0.4, market_p=0.5) == 0.0


def test_kelly_positive_edge_positive_fraction():
    f = kelly_fraction(p_win=0.7, market_p=0.5)
    assert f > 0
    assert f <= 1


def test_size_prediction_no_edge_rejected():
    p = Prediction(p_yes=0.5, confidence=0.9, key_features={},
                   brain_id="t", market_ticker="X")
    d = size_prediction(p, market_p=0.5, bankroll=1000)
    assert d.rejected and d.final_usd == 0


def test_size_prediction_with_edge_sized():
    p = Prediction(p_yes=0.7, confidence=0.8, key_features={},
                   brain_id="t", market_ticker="X")
    d = size_prediction(p, market_p=0.5, bankroll=1000)
    assert not d.rejected, d
    assert d.final_usd >= 5.0
    assert d.side == "yes"


def test_size_prediction_no_side():
    # Predict YES at 0.3 but market trades 0.5 → take NO side
    p = Prediction(p_yes=0.3, confidence=0.8, key_features={},
                   brain_id="t", market_ticker="X")
    d = size_prediction(p, market_p=0.5, bankroll=1000)
    assert d.side == "no", d


def test_size_prediction_per_trade_cap():
    # Max edge + huge bankroll, but capped by 5% rule, then ÷ MAE buffer
    p = Prediction(p_yes=0.99, confidence=1.0, key_features={},
                   brain_id="t", market_ticker="X")
    d = size_prediction(p, market_p=0.50, bankroll=10000)
    # 5% × 10000 = 500 cap, then ÷ 2 (MAE) = 250
    assert d.final_usd <= 250.0, d


if __name__ == "__main__":
    test_kelly_no_edge_returns_zero()
    test_kelly_positive_edge_positive_fraction()
    test_size_prediction_no_edge_rejected()
    test_size_prediction_with_edge_sized()
    test_size_prediction_no_side()
    test_size_prediction_per_trade_cap()
    print("OK test_sizer — 6/6 passed")
