"""Fill rate measured from observed flow, not assumed.

The placeholder it replaces (one full fill of the whole quote per horizon)
was an arbitrary constant that dominated every quote decision: at a
qualifying size it charged a full round-trip fee against a small reward, so
every market was rejected — and because nothing was ever quoted, the real
rate could never be learned. A circular refusal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.flow_stats import FlowStats


def test_unobserved_market_stays_unknown():
    f = FlowStats()
    assert f.expected_fills("T", 100, 86400) is None, \
        "an unobserved market produced a number instead of an unknown"


def test_a_short_observation_is_not_a_measurement():
    f = FlowStats(min_observation_sec=60)
    f.observe(ticker="T", contracts=10, ts=0)
    f.observe(ticker="T", contracts=10, ts=5)
    assert not f.measured("T")
    assert f.expected_fills("T", 100, 86400) is None


def test_rate_is_volume_over_the_observed_window():
    f = FlowStats(min_observation_sec=10)
    f.observe(ticker="T", contracts=100, ts=0)
    f.observe(ticker="T", contracts=100, ts=100)
    assert f.contracts_per_sec("T") == pytest.approx(2.0)


def test_queue_ahead_reduces_expected_fills():
    """Volume must clear the depth ahead of us before it reaches us."""
    f = FlowStats(min_observation_sec=10)
    f.observe(ticker="T", contracts=1000, ts=0)
    f.observe(ticker="T", contracts=1000, ts=100)     # 20/sec
    alone = f.expected_fills("T", 100, 100, queue_depth=0)
    behind = f.expected_fills("T", 100, 100, queue_depth=900)
    assert behind < alone
    assert behind == pytest.approx(2000 / 1000)


def test_bigger_quotes_fill_fewer_times():
    f = FlowStats(min_observation_sec=10)
    f.observe(ticker="T", contracts=1000, ts=0)
    f.observe(ticker="T", contracts=1000, ts=100)
    assert f.expected_fills("T", 1000, 100) < f.expected_fills("T", 10, 100)


def test_observe_trades_parses_public_trade_rows():
    f = FlowStats(min_observation_sec=0)
    n = f.observe_trades([
        {"ticker": "T", "count_fp": "25.00", "yes_price_dollars": "0.2000"},
        {"ticker": "T", "count_fp": "5", "yes_price_dollars": "0.2100"},
        {"ticker": "T", "count_fp": "0"},          # ignored
        {"count_fp": "10"},                        # no ticker, ignored
    ])
    assert n == 2
    assert f.summary()["T"]["contracts"] == 30.0


def test_zero_size_is_not_a_division_by_zero():
    f = FlowStats(min_observation_sec=10)
    f.observe(ticker="T", contracts=100, ts=0)
    f.observe(ticker="T", contracts=100, ts=100)
    assert f.expected_fills("T", 0, 100) is None
