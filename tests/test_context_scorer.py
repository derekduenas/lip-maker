"""Tests for engine/context_scorer.py — logit transform, heuristic fallback, prompt building."""

import math
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.context_scorer import _apply_context_delta, _heuristic_context_delta, score_term_context


def test_apply_context_delta_neutral():
    """Delta of 0 should return base rate unchanged."""
    result = _apply_context_delta(0.5, 0.0)
    assert result == pytest.approx(0.5, abs=0.001)


def test_apply_context_delta_positive():
    """Positive delta should increase probability."""
    base = 0.3
    result = _apply_context_delta(base, 1.0)
    assert result > base


def test_apply_context_delta_negative():
    """Negative delta should decrease probability."""
    base = 0.7
    result = _apply_context_delta(base, -1.0)
    assert result < base


def test_apply_context_delta_extreme_positive():
    """Max delta of +2.0 should stay below 1.0."""
    result = _apply_context_delta(0.9, 2.0)
    assert result < 1.0
    assert result > 0.9


def test_apply_context_delta_extreme_negative():
    """Min delta of -2.0 should stay above 0.0."""
    result = _apply_context_delta(0.1, -2.0)
    assert result > 0.0
    assert result < 0.1


def test_apply_context_delta_symmetry():
    """Opposite deltas on 50% base should be symmetric around 0.5."""
    up = _apply_context_delta(0.5, 1.0)
    down = _apply_context_delta(0.5, -1.0)
    assert up + down == pytest.approx(1.0, abs=0.001)


def test_heuristic_no_headlines():
    """No headlines should return delta 0."""
    result = _heuristic_context_delta("inflation", 0.5, [])
    assert result["context_delta"] == 0.0
    assert result["confidence"] == "low"


def test_heuristic_term_in_headlines():
    """Term appearing in headlines should get positive delta."""
    result = _heuristic_context_delta("tariffs", 0.3, [
        "New tariffs announced on Chinese imports",
        "Tariff impact on consumer prices",
    ])
    assert result["context_delta"] > 0


def test_heuristic_delta_capped():
    """Delta should be capped at 2.0 even with many mentions."""
    headlines = [f"tariffs tariffs tariffs headline {i}" for i in range(20)]
    result = _heuristic_context_delta("tariffs", 0.3, headlines)
    assert result["context_delta"] <= 2.0


def test_score_term_context_returns_required_fields():
    """score_term_context should return all required fields."""
    result = score_term_context(
        term="test_term",
        event_type="fomc_presser",
        event_date="2026-05-07",
        base_rate=0.5,
        recent_transcripts=[],
        news_headlines=["Test headline"],
    )
    assert "context_delta" in result
    assert "reasoning" in result
    assert "adjusted_prob" in result
    assert "confidence" in result
    assert isinstance(result["adjusted_prob"], float)
    assert 0 < result["adjusted_prob"] < 1
