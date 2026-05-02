"""Y5: PM scorer must penalize quotes not at top of book.

Quadratic spread decay: factor = ((max - distance) / max)^2.
At-best → 1.0; mid → 0.25; outside max_spread → 0.0.
"""
import importlib.util
import pytest
from pathlib import Path

# Load polymarket's rewards_schedule directly (avoid namespace clash with
# Kalshi's engine/ which is also on sys.path). Try both layouts: monorepo
# (/lip-maker/polymarket/...) and split-repo deploy (/polymarket-maker/...).
_CANDIDATE_PATHS = [
    Path(__file__).resolve().parent.parent / "polymarket" / "engine" / "rewards_schedule.py",
    Path("/root/polymarket-maker/engine/rewards_schedule.py"),
]
PM_RS_PATH = next((p for p in _CANDIDATE_PATHS if p.exists()), None)
if PM_RS_PATH is None:
    pytest.skip(f"polymarket rewards_schedule not found in any of {_CANDIDATE_PATHS}",
                allow_module_level=True)
import sys as _sys
_spec = importlib.util.spec_from_file_location("pm_rewards_schedule", PM_RS_PATH)
pm_rewards_schedule = importlib.util.module_from_spec(_spec)
_sys.modules["pm_rewards_schedule"] = pm_rewards_schedule  # @dataclass needs this
_spec.loader.exec_module(pm_rewards_schedule)


def _mock_schedule(monkeypatch):
    """Stub schedule_for_slug + current_period so we can control the math."""
    rs = pm_rewards_schedule

    class FakePeriod:
        pool_usd = 100.0
        target_size = 50
        discount_factor = 0.5

    class FakeSched:
        name = "test_category"

    def fake_schedule_for_slug(slug):
        return FakeSched()

    def fake_current_period(sched, hours):
        return ("test_period", FakePeriod())

    monkeypatch.setattr(rs, "schedule_for_slug", fake_schedule_for_slug)
    monkeypatch.setattr(rs, "current_period", fake_current_period)
    return rs


def test_at_best_factor_is_one(monkeypatch):
    rs = _mock_schedule(monkeypatch)
    r = rs.expected_daily_reward("slug", 24, our_size=50, top_of_book_size=50,
                                  our_distance_cents=0.0, max_spread_cents=2.0)
    assert r["spread_factor"] == 1.0
    assert r["raw_share"] == r["our_share"]


def test_one_tick_inside_decays_quadratic(monkeypatch):
    rs = _mock_schedule(monkeypatch)
    # 1¢ inside on max=2¢ → (1/2)^2 = 0.25
    r = rs.expected_daily_reward("slug", 24, our_size=50, top_of_book_size=50,
                                  our_distance_cents=1.0, max_spread_cents=2.0)
    assert r["spread_factor"] == 0.25
    assert r["our_share"] == round(r["raw_share"] * 0.25, 4)


def test_outside_max_spread_yields_zero(monkeypatch):
    rs = _mock_schedule(monkeypatch)
    r = rs.expected_daily_reward("slug", 24, our_size=50, top_of_book_size=50,
                                  our_distance_cents=2.5, max_spread_cents=2.0)
    assert r["spread_factor"] == 0.0
    assert r["our_share"] == 0.0
    assert r["expected_usd"] == 0.0


def test_default_args_preserve_old_behavior(monkeypatch):
    """Callers that don't pass distance/max get factor 1.0 (at-best assumption)."""
    rs = _mock_schedule(monkeypatch)
    r = rs.expected_daily_reward("slug", 24, our_size=50, top_of_book_size=50)
    assert r["spread_factor"] == 1.0
