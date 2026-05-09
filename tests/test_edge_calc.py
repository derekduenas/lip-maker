"""Tests for engine/edge_calc.py — fee calculation, Kelly sizing, edge signals."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.edge_calc import kalshi_maker_fee, kalshi_taker_fee, kelly_sizing, calculate_edge


# --- Fee tests ---

def test_maker_fee_at_midpoint():
    """At 50c, maker fee should be minimal (~0.000175)."""
    fee = kalshi_maker_fee(0.50)
    assert fee == pytest.approx(0.000175, abs=0.0001)


def test_maker_fee_at_extreme():
    """At 1c or 99c, maker fee should be near max (~0.004375)."""
    fee_low = kalshi_maker_fee(0.01)
    fee_high = kalshi_maker_fee(0.99)
    assert fee_low == pytest.approx(0.004375, abs=0.0002)
    assert fee_high == pytest.approx(0.004375, abs=0.0002)


def test_maker_cheaper_than_taker():
    """Maker fee should always be less than taker fee."""
    for price in [0.1, 0.3, 0.5, 0.7, 0.9]:
        assert kalshi_maker_fee(price) < kalshi_taker_fee(price)


# --- Kelly sizing tests ---

def test_kelly_positive_edge():
    """Positive edge should return non-zero Kelly fraction."""
    k, conf, hc, strat = kelly_sizing(0.6, 0.4, corpus_n=10)
    assert k > 0


def test_kelly_no_edge():
    """No edge or negative edge should return 0."""
    k, conf, hc, strat = kelly_sizing(0.3, 0.5, corpus_n=10)
    assert k == 0.0


def test_kelly_capped_at_max():
    """Kelly should never exceed HIGH_CONVICTION_MAX_PCT (25%)."""
    from config.settings import HIGH_CONVICTION_MAX_PCT
    k, conf, hc, strat = kelly_sizing(0.95, 0.10, corpus_n=50)
    assert k <= HIGH_CONVICTION_MAX_PCT


def test_kelly_half():
    """Normal trades use half-Kelly."""
    k, conf, hc, strat = kelly_sizing(0.7, 0.4, corpus_n=10)
    # Full Kelly = (3.5*0.9 - 0.1)/3.5 = 0.871...
    # Half Kelly = 0.436 -> capped at 0.15
    assert k <= 0.15


# --- calculate_edge integration tests ---

def test_calculate_edge_returns_signal():
    """calculate_edge should return a dict with signal field."""
    result = calculate_edge(
        term="inflation",
        event_type="fomc_presser",
        event_date="2026-05-07",
        kalshi_ticker="KXTEST",
        yes_price=0.50,
    )
    assert result is not None
    assert "signal" in result
    assert result["signal"] in ["BUY_YES", "BUY_NO", "SKIP"]


def test_calculate_edge_skip_on_no_data():
    """Should return SKIP for terms with no frequency data."""
    result = calculate_edge(
        term="nonexistent_term_xyz",
        event_type="fomc_presser",
        event_date="2026-05-07",
        kalshi_ticker="KXTEST",
        yes_price=0.50,
    )
    assert result["signal"] == "SKIP"


def test_calculate_edge_has_required_fields():
    """Edge result should contain all required fields."""
    result = calculate_edge(
        term="inflation",
        event_type="fomc_presser",
        event_date="2026-05-07",
        kalshi_ticker="KXTEST",
        yes_price=0.90,
    )
    required = ["ticker", "term", "signal", "edge", "kelly_fraction",
                "deploy_pct", "trade_side", "confidence", "market_price"]
    for field in required:
        assert field in result, f"Missing field: {field}"
