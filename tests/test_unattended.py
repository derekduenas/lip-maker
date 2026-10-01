"""24/7 unattended Kalshi loop. Paper and demo only."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_startup_cancels_every_order_before_the_first_quote():
    from mm.types import Side
    from mm.unattended.service import UnattendedSession
    from mm.venues.kalshi import KalshiAdapter

    adapter = KalshiAdapter(paper=True)
    placed = adapter.place(
        "KXBRENT-26OCT07", Side.YES, 40, 5, best_opposing_bid_cents=50)
    trace = []
    session = UnattendedSession(
        adapter=adapter,
        open_orders=[(placed["order_id"], "KXBRENT-26OCT07")],
        quote=lambda: trace.append("quote"),
        on_cancel=lambda n: trace.append(("cancel", n)),
    )
    session.start()
    assert trace[0][0] == "cancel"
    assert trace[0][1] == 1
    assert trace[1] == "quote"
    assert any(row["method"] == "DELETE" for row in adapter.sent)


def test_watchdog_restarts_after_a_crash_and_cancels_again():
    from mm.unattended.service import CrashWatchdog

    class Loop:
        def __init__(self):
            self.trace = []
            self.starts = 0

        def start(self):
            self.trace.append("cancel")
            self.trace.append("quote")
            self.starts += 1
            if self.starts == 1:
                raise RuntimeError("crash")

    loop = Loop()
    CrashWatchdog(lambda: loop, max_restarts=2).run()
    assert loop.trace == ["cancel", "quote", "cancel", "quote"]
    assert loop.starts == 2


def test_service_refuses_production_and_live_mode():
    from mm.unattended.service import UnattendedRefused, assert_paper_demo

    assert_paper_demo(paper=True, ws_url=None)
    assert_paper_demo(paper=True, ws_url="wss://demo-api.kalshi.co/trade-api/ws/v2")
    with pytest.raises(UnattendedRefused):
        assert_paper_demo(paper=False, ws_url=None)
    with pytest.raises(UnattendedRefused):
        assert_paper_demo(
            paper=True, ws_url="wss://api.elections.kalshi.com/trade-api/ws/v2")


def test_reference_move_of_one_tick_requotes_inside_a_second():
    from mm.unattended.feed import RequoteGate

    fired = []
    gate = RequoteGate(lambda market, price, latency: fired.append((market, price, latency)))
    gate.on_reference("KXBRENT-26OCT07", 50, now=10.0)
    assert fired == []
    gate.on_reference("KXBRENT-26OCT07", 50, now=10.2)
    assert fired == []
    gate.on_reference("KXBRENT-26OCT07", 51, now=10.4)
    assert fired == [("KXBRENT-26OCT07", 51, pytest.approx(0.0, abs=1e-6))]
    assert fired[0][2] < 1.0


def test_stale_websocket_falls_back_to_rest():
    from mm.unattended.feed import OrderbookFeed

    rest_calls = []

    def rest(market):
        rest_calls.append(market)
        return {"market": market, "yes": [(48, 10)]}

    feed = OrderbookFeed(rest_fetch=rest, stale_s=1.0)
    feed.note_ws("M", {"market": "M", "yes": [(50, 10)]}, now=0.0)
    assert feed.read("M", now=0.2)["yes"] == [(50, 10)]
    assert rest_calls == []
    assert feed.read("M", now=1.5)["yes"] == [(48, 10)]
    assert rest_calls == ["M"]
    assert feed.source["M"] == "rest"


def test_ws_book_drives_the_gate_from_the_lip_reference():
    from mm.unattended.feed import BookDriver

    fired = []
    driver = BookDriver(
        targets={"M": 100},
        on_requote=lambda market, price, latency: fired.append((market, price)),
    )
    driver.on_book("M", yes_bids=[(50, 30), (49, 100)], no_bids=[(50, 30)], now=1.0)
    assert fired == []
    # Reference on the yes side was 50 (30 >= target/5). One tick tighter.
    driver.on_book("M", yes_bids=[(51, 30), (49, 100)], no_bids=[(50, 30)], now=1.1)
    assert fired == [("M", 51)]


def test_size_optimizer_picks_the_interior_maximum_at_the_reference():
    from mm.selector import KalshiMarket
    from mm.unattended.optimize import optimize_sizes

    market = KalshiMarket(
        market="KXBRENT-26OCT07", series="KXBRENT",
        period_reward_usd=100.0, period_seconds=86400.0, seconds_left=86400.0,
        discount_factor=0.5, target_size=100.0,
        yes_bids=[(50, 100)], no_bids=[(50, 100)],
        days_to_settle=3, exchange_index=2, shard_cash_usd=10_000,
    )
    result = optimize_sizes(
        [market], bankroll=10_000, per_market_usd=10_000,
        per_event_usd=10_000, total_usd=10_000,
        sizes=(50, 100, 200), markout_usd_per_contract=0.10,
    )
    assert result.chosen[0].market == "KXBRENT-26OCT07"
    assert result.chosen[0].size == 100
    assert result.chosen[0].yes_cents == 50
    assert result.chosen[0].objective > result.objectives["KXBRENT-26OCT07"][200]


def test_quiet_multiday_is_funded_before_a_louder_program():
    from mm.selector import KalshiMarket
    from mm.unattended.optimize import optimize_sizes

    def row(name, pool, comp, days, period):
        return KalshiMarket(
            market=name, series=name.split("-")[0],
            period_reward_usd=pool, period_seconds=period, seconds_left=period,
            discount_factor=0.5, target_size=100.0,
            yes_bids=[(50, comp)], no_bids=[(50, comp)],
            days_to_settle=days, exchange_index=2, shard_cash_usd=10_000,
        )

    quiet = row("KXBRENT-26OCT07", pool=20, comp=50, days=3, period=3 * 86400)
    loud = row("KXBTC-26OCT02", pool=200, comp=400, days=1, period=86400)
    result = optimize_sizes(
        [loud, quiet], bankroll=10_000, per_market_usd=80,
        per_event_usd=80, total_usd=80,
        sizes=(50,), markout_usd_per_contract=0.0,
    )
    assert [row.market for row in result.chosen] == ["KXBRENT-26OCT07"]


def test_fifteen_minute_pools_stay_in_a_disabled_bucket():
    from mm.selector import KalshiMarket
    from mm.unattended.optimize import optimize_sizes

    short = KalshiMarket(
        market="KXFAST-26OCT01", series="KXFAST",
        period_reward_usd=500.0, period_seconds=15 * 60, seconds_left=15 * 60,
        discount_factor=0.5, target_size=100.0,
        yes_bids=[(50, 60)], no_bids=[(50, 60)],
        days_to_settle=1, exchange_index=2, shard_cash_usd=10_000,
    )
    held = optimize_sizes(
        [short], bankroll=10_000, per_market_usd=500,
        per_event_usd=500, total_usd=500, sizes=(50,),
        markout_usd_per_contract=0.0,
    )
    assert held.chosen == []
    assert held.short_pools == ["KXFAST-26OCT01"]
    opened = optimize_sizes(
        [short], bankroll=10_000, per_market_usd=500,
        per_event_usd=500, total_usd=500, sizes=(50,),
        markout_usd_per_contract=0.0, enable_short_pools=True,
    )
    assert [row.market for row in opened.chosen] == ["KXFAST-26OCT01"]


def test_caps_stop_the_optimizer():
    from mm.selector import KalshiMarket
    from mm.unattended.optimize import optimize_sizes

    markets = []
    for name in ("KXBRENT-26OCT07", "KXBRENT-26OCT08", "KXGOLD-26OCT07"):
        markets.append(KalshiMarket(
            market=name, series=name.split("-")[0],
            period_reward_usd=80.0, period_seconds=86400.0, seconds_left=86400.0,
            discount_factor=0.5, target_size=100.0,
            yes_bids=[(50, 10)], no_bids=[(50, 10)],
            days_to_settle=2, exchange_index=2, shard_cash_usd=10_000,
        ))
    result = optimize_sizes(
        markets, bankroll=100, per_market_usd=50, per_event_usd=50, total_usd=100,
        sizes=(50,), markout_usd_per_contract=0.0,
    )
    # 50c + 50c, size 50 → $50 capital. Per-event cap keeps the two Brent
    # markets from both filling. Total cap keeps the third from joining
    # once two $50 quotes are on.
    capital = sum(row.capital_usd for row in result.chosen)
    assert capital <= 100 + 1e-6
    brent = [row for row in result.chosen if row.market.startswith("KXBRENT")]
    assert len(brent) <= 1
    assert all(row.capital_usd <= 50 + 1e-6 for row in result.chosen)


def test_kill_switch_when_reward_does_not_cover_markout(tmp_path):
    from mm.unattended.health import Health

    cancelled = []
    alerts = []
    health = Health(
        heartbeat=tmp_path / "hb",
        cancel=lambda: cancelled.append("cancel"),
        alert=lambda payload: alerts.append(payload),
        webhook_url="https://alerts.example/hook",
    )
    health.record(0.0, reward_usd=4.0, markout_cost_usd=10.0)
    assert health.ratio(0.0) == pytest.approx(0.4)
    assert health.killed
    assert cancelled == ["cancel"]
    assert alerts and alerts[0]["reason"] == "reward_markout_ratio"
    health.record(10.0, reward_usd=100.0, markout_cost_usd=1.0)
    assert health.killed


def test_ratio_uses_a_rolling_day_and_ignores_an_empty_window():
    from mm.unattended.health import Health

    health = Health(heartbeat=None, cancel=lambda: None, alert=lambda payload: None)
    health.record(0.0, reward_usd=1.0, markout_cost_usd=10.0)
    assert health.killed is True
    assert health.ratio(86_400.0) is None
    health.reset()
    assert health.killed is False
    health.record(86_400.0, reward_usd=8.0, markout_cost_usd=2.0)
    assert health.ratio(86_400.0) == pytest.approx(4.0)
    assert health.killed is False


def test_daily_summary_and_heartbeat_and_optional_webhook(tmp_path, monkeypatch):
    from mm.unattended.health import Health, render_daily_summary

    text = render_daily_summary(day="2026-10-01", fills=3, pnl_usd=1.25, rewards_usd=4.0)
    assert "fills 3" in text
    assert "pnl_usd 1.2500" in text
    assert "rewards_usd 4.0000" in text

    sent = []
    monkeypatch.setenv("LIP_ALERT_WEBHOOK", "https://alerts.example/hook")
    health = Health(
        heartbeat=tmp_path / "hb",
        cancel=lambda: None,
        alert=lambda payload: sent.append(payload),
    )
    health.beat(now=123.0)
    assert (tmp_path / "hb").read_text(encoding="utf-8").strip() == "123.0"
    health.record(1.0, reward_usd=0.0, markout_cost_usd=2.0)
    assert sent and sent[0]["webhook"] == "https://alerts.example/hook"

    monkeypatch.delenv("LIP_ALERT_WEBHOOK", raising=False)
    quiet = []
    other = Health(heartbeat=None, cancel=lambda: None, alert=lambda payload: quiet.append(payload))
    other.record(1.0, reward_usd=0.0, markout_cost_usd=2.0)
    assert quiet == []


def test_deploy_artifacts_are_paper_demo_only():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    unit = (ROOT / "deploy" / "lip-unattended.service").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "UNATTENDED.md").read_text(encoding="utf-8")
    for text in (docker, unit):
        assert "LIP_PAPER=true" in text or "LIP_PAPER=true" in text.replace(" ", "")
        assert "LIP_PAPER=false" not in text
        assert "api.elections.kalshi.com" not in text
    assert "Restart=" in unit
    assert "us-east" in doc
    assert "15" in doc and "short" in doc.lower()
