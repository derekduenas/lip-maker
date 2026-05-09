"""Tests for prep/live_monitor.py — word parsing, detection, P&L math."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.live_monitor import (
    MarketSnapshot, _parse_word, classify_tick, estimate_live_pnl,
)


def test_parse_word_standard():
    assert _parse_word("Will Powell say Recession at his Apr 2026 press conference?") == "Recession"


def test_parse_word_slash():
    assert _parse_word("Will Powell say QE / Quantitative Easing at his Apr 2026 press conference?") \
        == "QE / Quantitative Easing"


def test_parse_word_no_match():
    assert _parse_word("Random title") is None


def test_classify_said_on_threshold_cross():
    prev = MarketSnapshot(ticker="T", yes_bid=50, yes_ask=60, volume=10, captured_at=0)
    curr = MarketSnapshot(ticker="T", yes_bid=88, yes_ask=92, volume=20, captured_at=30)
    result = classify_tick(prev, curr, warmup=False)
    assert result is not None
    event_type, price, jump = result
    assert event_type == "SAID"
    assert price >= 85


def test_classify_no_event_when_stable_high():
    """If already above SAID threshold, don't re-fire."""
    prev = MarketSnapshot(ticker="T", yes_bid=90, yes_ask=94, volume=10, captured_at=0)
    curr = MarketSnapshot(ticker="T", yes_bid=92, yes_ask=96, volume=15, captured_at=30)
    result = classify_tick(prev, curr, warmup=False)
    assert result is None


def test_classify_not_said_after_warmup():
    prev = MarketSnapshot(ticker="T", yes_bid=15, yes_ask=20, volume=5, captured_at=0)
    curr = MarketSnapshot(ticker="T", yes_bid=2, yes_ask=4, volume=10, captured_at=30)
    result = classify_tick(prev, curr, warmup=False)
    assert result is not None
    event_type, _, _ = result
    assert event_type == "NOT_SAID"


def test_classify_warmup_blocks_not_said():
    prev = MarketSnapshot(ticker="T", yes_bid=15, yes_ask=20, volume=5, captured_at=0)
    curr = MarketSnapshot(ticker="T", yes_bid=2, yes_ask=4, volume=10, captured_at=30)
    result = classify_tick(prev, curr, warmup=True)
    assert result is None


def test_classify_jump_event():
    prev = MarketSnapshot(ticker="T", yes_bid=40, yes_ask=50, volume=5, captured_at=0)
    curr = MarketSnapshot(ticker="T", yes_bid=65, yes_ask=75, volume=12, captured_at=30)
    result = classify_tick(prev, curr, warmup=False)
    assert result is not None
    event_type, _, jump = result
    assert event_type == "JUMP"
    assert jump is not None and jump >= 20


def test_estimate_pnl_yes_position_at_90c():
    """YES bet at 50c entry, now at 90c → big unrealized profit."""
    positions = {"KX-T": {"side": "yes", "price_cents": 50, "contracts": 100,
                           "cost_usd": 50.0, "status": "accepted"}}
    current = {"KX-T": MarketSnapshot(ticker="KX-T", yes_bid=88, yes_ask=92,
                                        volume=50, captured_at=0)}
    r = estimate_live_pnl(positions, current)
    assert r["total_cost"] == 50.0
    assert r["total_value"] == 90.0
    assert r["unrealized_pnl"] == 40.0


def test_estimate_pnl_no_position_wins_when_yes_drops():
    """NO bet at 50c entry (yes was 50c), yes now at 10c → NO wins big."""
    positions = {"KX-T": {"side": "no", "price_cents": 50, "contracts": 100,
                           "cost_usd": 50.0, "status": "accepted"}}
    current = {"KX-T": MarketSnapshot(ticker="KX-T", yes_bid=8, yes_ask=12,
                                        volume=30, captured_at=0)}
    r = estimate_live_pnl(positions, current)
    # value of NO = contracts × (100 - yes_mid) / 100 = 100 × 90 / 100 = 90
    assert r["total_value"] == 90.0
    assert r["unrealized_pnl"] == 40.0


def test_estimate_pnl_resolved_count():
    """Positions at yes_mid ≥ 98 or ≤ 2 count as resolved."""
    positions = {
        "A": {"side": "yes", "price_cents": 50, "contracts": 10, "cost_usd": 5, "status": "x"},
        "B": {"side": "no",  "price_cents": 50, "contracts": 10, "cost_usd": 5, "status": "x"},
        "C": {"side": "yes", "price_cents": 50, "contracts": 10, "cost_usd": 5, "status": "x"},
    }
    current = {
        "A": MarketSnapshot("A", 98, 100, 0, 0),    # resolved YES-win
        "B": MarketSnapshot("B", 0,  2,   0, 0),    # resolved NO-win
        "C": MarketSnapshot("C", 45, 55,  0, 0),    # not resolved
    }
    r = estimate_live_pnl(positions, current)
    assert r["n_likely_resolved"] == 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
