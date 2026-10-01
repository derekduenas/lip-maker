"""Droplet round: bankroll, venue headroom, horizon, rank, live accrual."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from mm.selector import adverse_selection_penalty, news_driven
from mm.session_gates import candidate_top, long_dated_event_days, min_hours_to_close
from mm.unattended.loop import (
    CANDIDATE_SIZE, VENUE_HEADROOM, RunLoop, _emit_clock, candidate_tickers,
    horizon_close_ts,
)


def _program(market, series, ts, *, reward=86400.0, days=30.0, category=""):
    return {
        "kind": "program", "market": market, "series": series,
        "period_reward_usd": reward, "period_seconds": 86400,
        "discount_factor": 0.5, "target_size": 100,
        "days_to_settle": days, "exchange_index": 2,
        "close_ts": ts + days * 86400,
        "start_ts": ts, "end_ts": ts + 86400,
        "category": category,
    }


def _snap(market, ts, seq):
    return {
        "type": "orderbook_snapshot", "sid": 1, "seq": seq, "ts": ts,
        "msg": {
            "market_ticker": market,
            "yes_dollars_fp": [["0.5000", "100.00"]],
            "no_dollars_fp": [["0.5000", "100.00"]],
        },
    }


def _frame(market, series, *, reward, days, category="", yes=None, no=None):
    row = {
        "market": market, "series": series, "period_reward_usd": reward,
        "period_seconds": 86400, "discount_factor": 0.5, "target_size": 100,
        "days_to_settle": days, "category": category,
    }
    if yes is not None:
        row["yes_bids"] = yes
    if no is not None:
        row["no_bids"] = no
    return row


def test_lip_bankroll_sets_the_venue_cap(monkeypatch):
    monkeypatch.setenv("LIP_BANKROLL", "5000")
    loop = RunLoop(mode="paper")
    assert loop.bankroll == 5000.0
    assert loop.risk.limits.per_venue_usd == Decimal("1500")
    assert loop._venue_budget() == float(Decimal("1500") * Decimal(str(VENUE_HEADROOM)))
    hardcoded = RunLoop(mode="paper", bankroll=10_000)
    assert hardcoded.bankroll == 10_000
    assert hardcoded.risk.limits.per_venue_usd == Decimal("3000")


def test_allocator_stops_at_venue_headroom():
    ts = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    loop = RunLoop(mode="paper", bankroll=5000, select_every=10**9)
    for i in range(20):
        market = f"KXEVT{i:02d}-1"
        # Higher pools rank first in both the allocator and the sizer,
        # so the names they share are the expensive ones.
        loop.on_frame(_program(market, f"KXEVT{i:02d}", ts, reward=1000 + i * 100))
        loop.on_frame(_snap(market, ts + 0.2, i + 1))
    loop._select(ts + 10)
    headroom = loop._venue_budget()
    assert headroom == 1425.0
    committed = float(loop.risk.venue_usd.get("kalshi", 0))
    # 50c + 50c at 100 contracts is $100. 14 fit under $1,425; 15 do not.
    assert len(loop.resting) == 14
    assert committed == 1400
    assert committed <= headroom
    assert "KXEVT00-1" not in loop.resting
    assert "KXEVT19-1" in loop.resting
    assert loop.kill is None


def test_cap_hit_skips_and_a_restart_clears_the_kill(caplog):
    caplog.set_level(logging.WARNING, logger="lip.readonly")
    ts = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    loop = RunLoop(mode="paper", bankroll=5000, select_every=10**9)
    market = "KXBRENT-26OCT07"
    loop.on_frame(_program(market, "KXBRENT", ts, days=10))
    loop.on_frame(_snap(market, ts + 1, 1))
    assert market in loop.resting
    assert loop.kill is None
    loop.on_frame(_program("KXOTHER-1", "KXOTHER", ts, days=10))
    loop.risk.venue_usd["kalshi"] = loop.risk.limits.per_venue_usd
    loop._quote("KXOTHER-1", 50, 50, 10, ts + 2)
    assert loop.kill is None
    assert loop.risk.killed is False
    assert market in loop.resting
    assert any("per_venue" in rec.message and "skip quote" in rec.message for rec in caplog.records)
    loop.risk.note_daily_pnl(-loop.risk.limits.daily_loss_usd)
    loop._quote(market, 50, 50, 10, ts + 3)
    assert loop.kill is not None
    assert loop.kill["cancel_all"] is True
    assert loop.resting == {}
    fresh = RunLoop(mode="paper", bankroll=5000)
    assert fresh.kill is None
    assert fresh.risk.killed is False


def test_defaults_are_48h_95d_and_1000_candidates(monkeypatch):
    assert min_hours_to_close() == 48
    assert long_dated_event_days() == 95
    assert candidate_top() == 1000
    monkeypatch.setenv("LIP_CANDIDATE_TOP", "4")
    assert candidate_top() == 4
    monkeypatch.delenv("LIP_CANDIDATE_TOP")
    monkeypatch.setenv("LIP_SUBSCRIBE_LIMIT", "7")
    assert candidate_top() == 7


def test_horizon_is_the_earlier_of_close_and_occurrence():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    close = (now + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    soon = (now + timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    later = (now + timedelta(days=10)).isoformat().replace("+00:00", "Z")
    early = horizon_close_ts({
        "close_time": close, "occurrence_datetime": soon,
    })
    assert early == datetime.fromisoformat(soon.replace("Z", "+00:00")).timestamp()
    from mm.unattended.loop import apply_market_meta
    from mm.selector import exclusion_reason, KalshiMarket
    frame = apply_market_meta(
        {"market": "KXFOO-1", "series": "KXFOO", "end_ts": (now + timedelta(days=20)).timestamp()},
        {"exchange_index": 2, "close_ts": early, "category": ""},
        now=now.timestamp(),
    )
    hours = frame["days_to_settle"] * 24
    assert hours == 30
    assert exclusion_reason(KalshiMarket(
        market="KXFOO-1", series="KXFOO", period_reward_usd=10, period_seconds=86400,
        seconds_left=86400, discount_factor=0.5, target_size=100,
        days_to_settle=frame["days_to_settle"], exchange_index=2,
    )) == "closes_within_24h"
    late_occ = horizon_close_ts({"close_time": close, "occurrence_datetime": later})
    assert late_occ == datetime.fromisoformat(close.replace("Z", "+00:00")).timestamp()
    only_occ = horizon_close_ts({"occurrence_datetime": later})
    assert only_occ == datetime.fromisoformat(later.replace("Z", "+00:00")).timestamp()


def test_candidates_rank_by_net_per_dollar_not_raw_pool():
    assert news_driven("Politics")
    assert news_driven("Entertainment") is True
    assert news_driven("Financials") is False
    assert adverse_selection_penalty(2, "News") > adverse_selection_penalty(2, "")
    assert adverse_selection_penalty(2, "") > adverse_selection_penalty(40, "")
    crowded = _frame(
        "KXBRENT-CROWD", "KXBRENT", reward=50_000, days=30,
        yes=[(50, 10_000)], no=[(50, 10_000)],
    )
    ours = _frame("KXBRENT-OURS", "KXBRENT", reward=2_000, days=30)
    far = _frame("KXBRENT-FAR", "KXBRENT", reward=2_000, days=40)
    near = _frame("KXBRENT-NEAR", "KXBRENT", reward=2_000, days=3)
    news = _frame("KXFOO-NEWS", "KXFOO", reward=2_000, days=40, category="Politics")
    match = _frame("KXTTELITEMATCH-1", "KXTTELITEMATCH", reward=10**9, days=40)
    names = candidate_tickers([crowded, ours, far, near, news, match])
    # The crowded book has the larger pool. Net per dollar still prefers the book we can share.
    assert names.index("KXBRENT-OURS") < names.index("KXBRENT-CROWD")
    assert names.index("KXBRENT-FAR") < names.index("KXBRENT-NEAR")
    assert names.index("KXBRENT-FAR") < names.index("KXFOO-NEWS")
    assert "KXTTELITEMATCH-1" not in names
    assert CANDIDATE_SIZE == 100


def test_resting_quotes_accrue_on_the_live_clock():
    ts = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    loop = RunLoop(mode="paper", bankroll=5000, select_every=10**9)
    market = "KXBRENT-26OCT07"
    loop.on_frame(_program(market, "KXBRENT", ts, reward=10, days=10))
    loop.on_frame(_snap(market, ts + 0.2, 1))
    assert market in loop.resting
    quiet = Decimal(loop.live_status()["estimated_usd"])
    for step in range(1, 6):
        loop.on_frame({"type": "clock", "ts": ts + step + 0.2})
    accrued = Decimal(loop.live_status()["estimated_usd"])
    assert accrued > quiet
    assert accrued > 0
    # The $1 period floor would still hide this partial. The live figure is raw.
    assert loop.accruals[market].estimate().estimated_usd == "0"
    scored_before = sum(1 for mark in loop.accruals[market].marks if mark.status == "scored")
    loop.on_frame({"type": "clock", "ts": ts + 300})
    scored_after = sum(1 for mark in loop.accruals[market].marks if mark.status == "scored")
    missed = sum(1 for mark in loop.accruals[market].marks if mark.status == "missed")
    assert scored_after <= scored_before + 1
    assert missed > 100
    assert loop.finish != loop.live_status


def test_clock_task_emits_a_frame():
    seen = []

    async def _run():
        task = asyncio.create_task(_emit_clock(seen.append))
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    import mm.unattended.loop as loop_mod
    previous = loop_mod.CLOCK_INTERVAL_S
    loop_mod.CLOCK_INTERVAL_S = 0.01
    try:
        asyncio.run(_run())
    finally:
        loop_mod.CLOCK_INTERVAL_S = previous
    assert seen
    assert seen[0]["type"] == "clock"
    assert seen[0]["ts"] > 0
