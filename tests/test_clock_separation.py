"""Reward expiry and market settlement are different clocks.

Measured on live markets, they diverge by a week:

    KXTTELITEMATCH-26SEP210030ASOMLU-ASO
        incentive ends in     86 minutes
        market closes in  10,106 minutes

Conflating them errs both ways: treating reward expiry as settlement
flattens inventory in a market that trades for another week; treating
settlement as reward expiry keeps quoting for a reward that has stopped.

The ticker-string parser this replaces was wrong by days on real tickers,
and returned None on others — and None meant the pre-settlement gate did
not fire while the economics assumed 24 hours to settle.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.entry_cutoff import (
    ALL_POLICIES, CONTROL_CUTOFF_MIN, MAX_MIN, MIN_MIN, POLICY_CONTROL,
    POLICY_FIXED_60S, POLICY_PROPORTIONAL, cutoff_minutes, describe,
    policy_fingerprint)
from engine.market_clock import MarketClock, SOURCE_API, UNKNOWN


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ── the clock ─────────────────────────────────────────────────────────────

def test_close_time_comes_from_the_venue():
    now = time.time()
    c = MarketClock(fetcher=lambda t: {"open_time": _iso(now - 600),
                                       "close_time": _iso(now + 900)})
    ct = c.close_time("T")
    assert ct.source == SOURCE_API and ct.known
    assert ct.minutes_until() == pytest.approx(15.0, abs=0.2)


def test_duration_is_the_markets_own_window():
    now = time.time()
    c = MarketClock(fetcher=lambda t: {"open_time": _iso(now - 900),
                                       "close_time": _iso(now + 900)})
    assert c.close_time("T").duration_min == pytest.approx(30.0, abs=0.2)


def test_unknown_close_is_unknown_not_far_away():
    """The old failure: None was read as 'no settlement risk'."""
    c = MarketClock(fetcher=lambda t: {})
    ct = c.close_time("T")
    assert not ct.known and ct.source == UNKNOWN
    assert ct.minutes_until() is None
    assert c.minutes_until_close("T") is None


def test_a_fetch_failure_does_not_invent_a_time():
    def boom(_):
        raise RuntimeError("network down")
    c = MarketClock(fetcher=boom)
    assert c.minutes_until_close("T") is None
    assert c.fetch_errors == 1


def test_ticker_parsing_is_off_by_default():
    """It was measurably wrong by days; opt-in only, and always labelled."""
    c = MarketClock(fetcher=lambda t: {})
    assert c.allow_ticker_fallback is False
    assert c.minutes_until_close("T", ticker_parse=lambda t: 123.0) is None


def test_ticker_fallback_is_labelled_when_enabled():
    from engine.market_clock import SOURCE_TICKER
    c = MarketClock(fetcher=lambda t: {}, allow_ticker_fallback=True)
    ct = c.close_time("T", ticker_parse=lambda t: 10.0)
    assert ct.source == SOURCE_TICKER and ct.known


def test_reward_window_is_not_the_settlement_clock():
    """A program can end long before the contract closes. The clock must
    report the CONTRACT's close, not the reward's end."""
    now = time.time()
    reward_ends = now + 86 * 60
    market_closes = now + 10_106 * 60          # the measured divergence
    c = MarketClock(fetcher=lambda t: {"open_time": _iso(now - 60),
                                       "close_time": _iso(market_closes)})
    mins = c.minutes_until_close("T")
    assert mins == pytest.approx(10_106, abs=2)
    assert mins > (reward_ends - now) / 60


# ── the frozen policies ───────────────────────────────────────────────────

def test_control_is_unchanged_at_thirty_minutes():
    for d in (None, 15.0, 60.0, 10_000.0):
        assert cutoff_minutes(POLICY_CONTROL, d).cutoff_min == CONTROL_CUTOFF_MIN


def test_fixed_policy_is_exactly_sixty_seconds():
    assert cutoff_minutes(POLICY_FIXED_60S, 15.0).cutoff_min == 1.0


@pytest.mark.parametrize("duration,expected", [
    (15.0, 3.0),        # 0.20 * 15
    (60.0, 12.0),       # 0.20 * 60
    (600.0, 30.0),      # clamped at MAX_MIN
    (2.0, 1.0),         # clamped at MIN_MIN
])
def test_proportional_formula_is_exact(duration, expected):
    assert cutoff_minutes(POLICY_PROPORTIONAL, duration).cutoff_min == expected


def test_proportional_never_exceeds_the_control():
    """No variant may be MORE conservative than today's live setting."""
    for d in (1.0, 10.0, 100.0, 100_000.0):
        assert cutoff_minutes(POLICY_PROPORTIONAL, d).cutoff_min <= CONTROL_CUTOFF_MIN


def test_proportional_respects_its_floor():
    for d in (0.5, 1.0, 4.0):
        assert cutoff_minutes(POLICY_PROPORTIONAL, d).cutoff_min >= MIN_MIN


def test_unknown_duration_falls_back_to_the_control_not_to_permissive():
    d = cutoff_minutes(POLICY_PROPORTIONAL, None)
    assert d.cutoff_min == CONTROL_CUTOFF_MIN and "control" in d.basis


def test_bounds_are_explicit_and_stated():
    assert (MIN_MIN, MAX_MIN) == (1.0, 30.0)
    txt = describe()["policies"][POLICY_PROPORTIONAL]
    assert "min(" in txt and "max(" in txt and "duration_min" in txt


def test_policies_are_fingerprinted_for_preregistration():
    """The fingerprint is recorded with results so a reader can tell whether
    the constants were edited after seeing which arm won."""
    assert policy_fingerprint() == "be3ad636dcbe6549"
    assert set(ALL_POLICIES) == {POLICY_CONTROL, POLICY_FIXED_60S,
                                 POLICY_PROPORTIONAL}


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError):
        cutoff_minutes("whatever_wins", 10.0)
