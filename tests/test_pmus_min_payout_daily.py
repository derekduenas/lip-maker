"""PM US screen (2026-10-02): markets whose split pool can never reach PM US's
$1 minimum payout do not take candidate slots, and the "daily" period type
(polymarket.us/rewards label "Daily") is a per-day pool like daily_event."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mm.unattended import pmus_paper as P

NOW = datetime(2026, 10, 1, 22, 0, tzinfo=ZoneInfo("UTC")).timestamp()


def _rec(slug, pid="p1", period="daily_event", pool=900.0, **extra):
    tp = {"programId": pid, "programType": "liquidityProgram", "status": "active",
          "start": "2026-10-01T00:00:00Z", "rewardPool": pool, "discountFactor": 0.5,
          "targetSize": 200, "period": period}
    tp.update(extra)
    return {"marketSlug": slug, "instrumentState": "INSTRUMENT_STATE_OPEN", "category": "CUL",
            "timePeriods": [tp]}


def _meta(close_days=15):
    return {"close_ts": NOW + close_days * 86400, "tick": 0.01, "market_type": "futures",
            "category": "culture", "active": True, "closed": False, "occurrence_ts": None,
            "best_bid": 0.3, "best_ask": 0.56, "fetched": NOW}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("LIP_PMUS_PERIODS", raising=False)
    monkeypatch.setenv("LIP_PMUS_POOL_SPLIT", "members")
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")


def test_max_payable_usd():
    assert P.max_payable_usd(5.0, 86400.0) == pytest.approx(5.0)
    assert P.max_payable_usd(30.0, 10 * 86400.0) == pytest.approx(3.0)   # per day on long windows
    assert P.max_payable_usd(0.5, 3600.0) == pytest.approx(0.5)          # whole short period
    assert P.max_payable_usd(0.0, 86400.0) == 0.0
    assert P.max_payable_usd(5.0, 0.0) == 0.0


def test_shared_pool_under_one_dollar_is_screened_out_before_meta():
    # $2/day shared by 4 members = $0.50/market/day: can never pay (docs: under $1 not paid)
    small = [_rec(f"rtc-small-2026-10-20-{c}", pid="small", pool=2.0) for c in "abcd"]
    big = [_rec(f"rtc-big-2026-10-20-{c}", pid="big", pool=10.0) for c in "ab"]
    frames, stats, need = P.records_to_programs(small + big, {r["marketSlug"]: _meta() for r in big}, now=NOW)
    assert stats["reasons"].get("below_min_payout") == 4
    assert sorted(f["market"] for f in frames) == ["PMUS:rtc-big-2026-10-20-a", "PMUS:rtc-big-2026-10-20-b"]
    assert all(f["period_reward_usd"] == pytest.approx(5.0) for f in frames)
    assert not [s for s in need if "small" in s]  # no metadata fetch spent on them


def test_min_payout_uses_the_split_mode(monkeypatch):
    recs = [_rec(f"rtc-small-2026-10-20-{c}", pid="small", pool=2.0) for c in "abcd"]
    meta = {r["marketSlug"]: _meta() for r in recs}
    monkeypatch.setenv("LIP_PMUS_POOL_SPLIT", "market")
    frames, stats, _ = P.records_to_programs(recs, meta, now=NOW)
    assert len(frames) == 4 and "below_min_payout" not in stats["reasons"]


def test_daily_period_is_a_per_day_pool_like_daily_event():
    d0, d1 = P.et_day_bounds(NOW)
    win = P.program_window({"period": "daily", "start": "2026-09-24T02:00:00Z"}, None, NOW + 86400 * 30, NOW)
    assert win == (d0, d1, d1 - d0)
    recs = [_rec("cpc-btc-2026-10-20-a", pid="crypto", period="daily", pool=40.0)]
    frames, stats, _ = P.records_to_programs(recs, {"cpc-btc-2026-10-20-a": _meta()}, now=NOW)
    assert [f["pm_period"] for f in frames] == ["daily"]
    assert frames[0]["period_seconds"] == pytest.approx(d1 - d0)
    assert "period_not_allowed" not in stats["reasons"]


def test_daily_follows_daily_event_in_the_period_filter(monkeypatch):
    monkeypatch.setenv("LIP_PMUS_PERIODS", "daily_event,early,pre_day")
    assert "daily" in P.allowed_periods()
    monkeypatch.setenv("LIP_PMUS_PERIODS", "early,pre_day")
    assert "daily" not in P.allowed_periods()
    assert P.program_window({"period": "daily", "start": "2026-09-24T02:00:00Z"}, None, NOW + 86400, NOW) is None
