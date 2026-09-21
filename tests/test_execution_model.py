"""Our executions, estimated from trades that could actually have hit us.

Replaces engine/flow_stats.py, which divided TOTAL market volume by
(queue + size) and fed that in as a fill rate. That counted flow on the
side we were not quoting and at prices our order could never be reached
at, and public volume does not establish the probability that our specific
order fills — it only bounds it from above.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.execution_model import CONSERVATIVE_QUEUE_MULT, ExecutionModel

T = "KXEXEC-26SEP30-T1"


def _m(**kw):
    return ExecutionModel(min_observation_sec=10, latency_sec=0.25, **kw)


def _hit_yes_bid(m, qty, price=45, ts=0.0):
    """A taker buying NO at (100-price) consumes our YES bid at `price`."""
    m.observe_trade(ticker=T, contracts=qty, yes_price_cents=price,
                    no_price_cents=100 - price, taker_side="no", ts=ts)


def _est(m, **kw):
    args = dict(ticker=T, side="yes", price_cents=45, size=50.0,
                horizon_sec=100.0, queue_ahead_displayed=0.0)
    args.update(kw)
    return m.estimate(**args)


# ── what counts ───────────────────────────────────────────────────────────

def test_only_trades_at_our_price_level_count():
    m = _m()
    _hit_yes_bid(m, 100, price=45, ts=0)
    _hit_yes_bid(m, 900, price=46, ts=100)      # a level we do not rest at
    assert _est(m).eligible_contracts == 100.0


def test_only_takers_hitting_our_side_count():
    """A taker buying YES lifts offers; it never trades against our YES bid."""
    m = _m()
    _hit_yes_bid(m, 100, ts=0)
    m.observe_trade(ticker=T, contracts=900, yes_price_cents=45,
                    no_price_cents=55, taker_side="yes", ts=100)
    assert _est(m).eligible_contracts == 100.0


def test_a_no_side_order_is_reached_by_yes_takers():
    m = _m()
    m.observe_trade(ticker=T, contracts=200, yes_price_cents=45,
                    no_price_cents=55, taker_side="yes", ts=0)
    m.observe_trade(ticker=T, contracts=1, yes_price_cents=45,
                    no_price_cents=55, taker_side="yes", ts=100)
    e = _est(m, side="no", price_cents=55)
    assert e.eligible_contracts == 201.0


# ── depth, latency, size ──────────────────────────────────────────────────

def test_depth_ahead_must_clear_before_we_execute():
    m = _m()
    _hit_yes_bid(m, 100, ts=0)
    _hit_yes_bid(m, 100, ts=100)                 # 2/sec over 100s = 200
    assert _est(m, queue_ahead_displayed=0).base == pytest.approx(200 / 50, rel=1e-2)
    assert _est(m, queue_ahead_displayed=200).base == 0.0


def test_latency_reduces_the_usable_horizon():
    fast = ExecutionModel(min_observation_sec=10, latency_sec=0.0)
    slow = ExecutionModel(min_observation_sec=10, latency_sec=50.0)
    for m in (fast, slow):
        _hit_yes_bid(m, 100, ts=0)
        _hit_yes_bid(m, 100, ts=100)
    assert _est(slow).base < _est(fast).base


def test_a_bigger_order_is_filled_fewer_times_by_the_same_flow():
    m = _m()
    _hit_yes_bid(m, 500, ts=0)
    _hit_yes_bid(m, 500, ts=100)
    assert _est(m, size=1000.0).base < _est(m, size=10.0).base


# ── sensitivity ───────────────────────────────────────────────────────────

def test_three_queue_cases_are_reported_not_one_number():
    m = _m()
    _hit_yes_bid(m, 300, ts=0)
    _hit_yes_bid(m, 300, ts=100)
    e = _est(m, queue_ahead_displayed=100)
    assert e.optimistic > e.base > e.conservative
    assert e.conservative == pytest.approx(
        max(0.0, 600 - 100 * CONSERVATIVE_QUEUE_MULT) / 50, rel=1e-2)


def test_the_estimate_never_claims_to_be_a_measured_fill_rate():
    m = _m()
    _hit_yes_bid(m, 100, ts=0)
    _hit_yes_bid(m, 100, ts=100)
    basis = _est(m).explain()["basis"]
    assert "UPPER BOUND" in basis and "not a measured fill rate" in basis


# ── unknown stays unknown ─────────────────────────────────────────────────

def test_an_unobserved_market_is_unknown_not_zero():
    e = _est(_m())
    assert e.measured is False
    assert e.base is None and e.optimistic is None and e.conservative is None


def test_a_short_observation_is_not_a_measurement():
    m = ExecutionModel(min_observation_sec=60, latency_sec=0.0)
    _hit_yes_bid(m, 100, ts=0)
    _hit_yes_bid(m, 100, ts=5)
    assert not m.measured(T) and _est(m).base is None


def test_zero_size_is_not_a_division_by_zero():
    m = _m()
    _hit_yes_bid(m, 100, ts=0)
    _hit_yes_bid(m, 100, ts=100)
    assert _est(m, size=0.0).base is None


def test_observe_trades_parses_public_rows():
    m = _m()
    n = m.observe_trades([
        {"ticker": T, "count_fp": "25.00", "yes_price_dollars": "0.4500",
         "no_price_dollars": "0.5500", "taker_side": "no"},
        {"ticker": T, "count_fp": "0"},          # ignored
        {"count_fp": "10"},                      # no ticker, ignored
    ])
    assert n == 1
