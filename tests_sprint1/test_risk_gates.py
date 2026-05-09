"""Tests for prep/risk_gates.py — safety net must be bulletproof."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.risk_gates import OrderCandidate, check_gates


def _good_order(**overrides):
    base = dict(
        ticker="KXFEDMENTION-26APR-IRAN",
        side="no",
        price_cents=50,
        contracts=10,
        bankroll_usd=5000.0,
        event_id="fomc_20260430",
        event_exposure_usd=0.0,
        thesis_generated_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    base.update(overrides)
    return OrderCandidate(**base)


def _with_live_env(monkeypatch, **extra):
    """Set SOV_LIVE=true + any extras so gates can progress past G2."""
    monkeypatch.setenv("SOV_LIVE", "true")
    for k, v in extra.items():
        monkeypatch.setenv(k, str(v))


def test_gate_1_kill_switch_blocks(monkeypatch):
    monkeypatch.setenv("SOV_LIVE", "true")
    monkeypatch.setenv("SOV_KILL", "true")
    ok, reason = check_gates(_good_order(), kalshi_balance_usd=1000)
    assert not ok and "KILL_SWITCH" in reason


def test_gate_2_paper_default(monkeypatch):
    """No SOV_LIVE → rejection with G2 reason."""
    monkeypatch.delenv("SOV_LIVE", raising=False)
    monkeypatch.delenv("SOV_KILL", raising=False)
    ok, reason = check_gates(_good_order(), kalshi_balance_usd=1000)
    assert not ok and "MODE" in reason


def test_gate_3_balance_floor(monkeypatch):
    _with_live_env(monkeypatch)
    ok, reason = check_gates(_good_order(), kalshi_balance_usd=20.0)
    assert not ok and "BALANCE_FLOOR" in reason


def test_gate_4_per_position_cap(monkeypatch):
    _with_live_env(monkeypatch)
    # 8% of $5k = $400. Order cost 600 → should reject.
    order = _good_order(price_cents=60, contracts=1000)  # $600
    ok, reason = check_gates(order, kalshi_balance_usd=5000)
    assert not ok and "PER_POSITION" in reason


def test_gate_5_per_event_cap(monkeypatch):
    _with_live_env(monkeypatch)
    # 30% of $5k = $1500. Existing exposure $1450, new order $100 → reject.
    order = _good_order(price_cents=50, contracts=200, event_exposure_usd=1450)
    ok, reason = check_gates(order, kalshi_balance_usd=5000)
    assert not ok and "PER_EVENT" in reason


def test_gate_6_daily_loss_halt(monkeypatch):
    _with_live_env(monkeypatch)
    # 5% of $5k = -$250. Daily P&L -$300 → halt.
    ok, reason = check_gates(_good_order(), kalshi_balance_usd=5000,
                              daily_realized_pnl=-300)
    assert not ok and "DAILY_LOSS" in reason


def test_gate_7_single_fill_halt(monkeypatch):
    _with_live_env(monkeypatch)
    # 2% of $5k = -$100. Worst single -$150 → halt.
    ok, reason = check_gates(_good_order(), kalshi_balance_usd=5000,
                              worst_single_pnl=-150)
    assert not ok and "SINGLE_LOSS" in reason


def test_gate_8_min_size(monkeypatch):
    _with_live_env(monkeypatch)
    ok, reason = check_gates(_good_order(contracts=2), kalshi_balance_usd=5000)
    assert not ok and "MIN_SIZE" in reason


def test_gate_9_price_sanity(monkeypatch):
    _with_live_env(monkeypatch)
    ok, reason = check_gates(_good_order(price_cents=0), kalshi_balance_usd=5000)
    assert not ok and "PRICE_SANITY" in reason

    ok, reason = check_gates(_good_order(price_cents=100), kalshi_balance_usd=5000)
    assert not ok and "PRICE_SANITY" in reason


def test_gate_10_thesis_freshness(monkeypatch):
    _with_live_env(monkeypatch)
    old_ts = datetime.now(timezone.utc) - timedelta(hours=48)
    ok, reason = check_gates(_good_order(thesis_generated_at=old_ts),
                              kalshi_balance_usd=5000)
    assert not ok and "THESIS_FRESHNESS" in reason


def test_all_gates_pass(monkeypatch):
    _with_live_env(monkeypatch)
    ok, reason = check_gates(_good_order(), kalshi_balance_usd=5000)
    assert ok, f"expected pass but got: {reason}"


def test_custom_caps_via_env(monkeypatch):
    """Overriding caps via env vars works."""
    _with_live_env(monkeypatch, SOV_MAX_POSITION_PCT="0.50")
    # 50% of $5k = $2500. Order $600 → now ALLOWED.
    order = _good_order(price_cents=60, contracts=1000)
    ok, _ = check_gates(order, kalshi_balance_usd=5000)
    assert ok


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
