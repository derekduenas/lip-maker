"""Close pull, intraday exclusion, fill cap, and the live series gate."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

from execution.quote_manager import QuoteManager, QuoteTarget, RestingOrder
from mm.safety.supervisor import supervisor_once, write_heartbeat
from mm.selector import KalshiMarket, allocate, exclusion_reason
from mm.session_gates import (
    SeriesGateConfig, SeriesStats, inside_close_window, max_contracts_for_fill,
    series_go,
)
from mm.unattended.optimize import optimize_sizes


def _market(name: str, series: str, *, price: int = 50) -> KalshiMarket:
    return KalshiMarket(
        market=name, series=series,
        period_reward_usd=100, period_seconds=86400, seconds_left=86400,
        discount_factor=0.5, target_size=100,
        yes_bids=[(price, 100)], no_bids=[(price, 100)],
        days_to_settle=3, exchange_index=2, shard_cash_usd=10_000,
    )


def _passing() -> SeriesStats:
    return SeriesStats(
        series="KXBRENT", days=5, settled_fills=30,
        net_usd=70, reward_usd=100, markout_5m_usd=30,
    )


def test_close_boundary_is_fifteen_minutes():
    close = 10_000.0
    assert inside_close_window(close, close - 15 * 60, pull_before_s=15 * 60)
    assert not inside_close_window(close, close - 15 * 60 - 1, pull_before_s=15 * 60)


def test_quoter_cancels_inside_the_close_window(tmp_path):
    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
    qm._update_quote_status = MagicMock()
    qm.resting["MKT"] = [RestingOrder(
        "oid", "MKT", "yes", 50, 10, 0.0, client_order_id="coid-1",
    )]
    report = qm.reconcile(QuoteTarget(
        "MKT", 50, 50, 10, close_ts=time.time() + 10 * 60,
    ))
    assert report["reason"] == "close_cutoff"
    assert report["cancelled"] == 1
    assert not qm.resting.get("MKT")


def test_supervisor_cancels_a_market_inside_the_close_window(tmp_path):
    heartbeat = tmp_path / "hb"
    log = tmp_path / "cancel"
    write_heartbeat(heartbeat, now=1_000)
    code = supervisor_once(
        heartbeat, log, stale_ms=10_000, now=1_000,
        closes={"KXBRENT-26OCT07": 1_000 + 14 * 60},
        pull_before_s=15 * 60,
    )
    assert code == 0
    assert log.read_text(encoding="utf-8").strip() == "cancel KXBRENT-26OCT07"
    quiet = tmp_path / "quiet"
    assert supervisor_once(
        heartbeat, quiet, stale_ms=10_000, now=1_000,
        closes={"KXBRENT-26OCT07": 1_000 + 16 * 60},
        pull_before_s=15 * 60,
    ) == 0
    assert not quiet.exists()


def test_hourly_and_fifteen_minute_series_stay_out_unless_enabled():
    hourly = _market("KXTEMPMIAH-26SEP2101-T72.99", "KXTEMPMIAH")
    quarter = _market("KXBTC15M-26OCT011200", "KXBTC15M")
    weekly = _market("KXBRENT-26OCT07", "KXBRENT")
    assert exclusion_reason(hourly) == "intraday_hourly"
    assert exclusion_reason(quarter) == "intraday_15m"
    assert exclusion_reason(weekly) == ""
    assert exclusion_reason(hourly, allow_intraday=True) == ""
    blocked = allocate([hourly, quarter, weekly], bankroll=10_000, chunk=100, max_size=100,
                       per_market_usd=10_000, per_series_usd=10_000, per_category_usd=10_000)
    reasons = dict(blocked.excluded)
    assert reasons["KXTEMPMIAH-26SEP2101-T72.99"] == "intraday_hourly"
    assert reasons["KXBTC15M-26OCT011200"] == "intraday_15m"
    assert "KXBRENT-26OCT07" not in reasons
    opened = allocate([hourly], bankroll=10_000, chunk=100, max_size=100,
                      per_market_usd=10_000, per_series_usd=10_000, per_category_usd=10_000,
                      allow_intraday=True)
    assert "KXTEMPMIAH-26SEP2101-T72.99" not in dict(opened.excluded)


def test_single_fill_cap_sizes_the_quote_before_it_is_sent(tmp_path):
    assert max_contracts_for_fill(50) == 200
    assert max_contracts_for_fill(80) == 125
    assert max_contracts_for_fill(50, 100) * 50 <= 10_000
    market = _market("KXBRENT-26OCT07", "KXBRENT", price=80)
    selection = allocate(
        [market], bankroll=10_000, chunk=100, max_size=400,
        per_market_usd=10_000, per_series_usd=10_000, per_category_usd=10_000,
    )
    assert selection.taken
    assert selection.taken[-1].size == 100
    assert selection.taken[-1].size <= 125

    plan = optimize_sizes(
        [market], bankroll=10_000, per_market_usd=10_000,
        per_event_usd=10_000, total_usd=10_000,
        sizes=(200,), markout_usd_per_contract=0.0,
    )
    assert plan.chosen[0].size == 125

    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
    qm._passes_safety = lambda target: (True, "ok")
    placed = []
    qm._place_order = lambda *args, **kwargs: placed.append(args) or None
    qm.reconcile(QuoteTarget("MKT", 80, 20, 500))
    sizes = {row[1]: row[3] for row in placed}
    assert sizes["yes"] == 125
    assert sizes["no"] == 500
    assert 80 / 100 * sizes["yes"] <= 100
    assert 20 / 100 * sizes["no"] <= 100


def test_live_selector_trades_only_a_series_that_passes_the_gate():
    assert series_go(_passing()) == (True, "go")
    assert series_go(SeriesStats("KXBRENT", 4, 30, 70, 100, 30))[1] == "days"
    assert series_go(SeriesStats("KXBRENT", 5, 29, 70, 100, 30))[1] == "fills"
    assert series_go(SeriesStats("KXBRENT", 5, 30, 0, 100, 30))[1] == "net"
    assert series_go(SeriesStats("KXBRENT", 5, 30, 70, 100, 100))[1] == "markout"
    assert series_go(SeriesStats("KXBRENT", 5, 30, 40, 100, 20))[1] == "haircut"
    assert series_go(None)[1] == "no_series_record"

    good = _market("KXBRENT-26OCT07", "KXBRENT")
    bad = _market("KXETH-26OCT07", "KXETH")
    stats = {"KXBRENT": _passing()}
    live = allocate(
        [good, bad], bankroll=10_000, chunk=100, max_size=100,
        per_market_usd=10_000, per_series_usd=10_000, per_category_usd=10_000,
        live=True, series_stats=stats, series_gate=SeriesGateConfig(),
    )
    reasons = dict(live.excluded)
    assert reasons["KXETH-26OCT07"] == "series_gate:no_series_record"
    assert "KXBRENT-26OCT07" not in reasons
    paper = allocate(
        [bad], bankroll=10_000, chunk=100, max_size=100,
        per_market_usd=10_000, per_series_usd=10_000, per_category_usd=10_000,
        live=False, series_stats={},
    )
    assert "KXETH-26OCT07" not in dict(paper.excluded)
