"""Kalshi arming, disconnect safety, and the pool selector."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.lip_scorer import ProgramParams, interval_payout_usd, kalshi_period_payout
from mm.safety.fix_session import (
    MockKalshiFixAcceptor, MockPMUSFixGateway, kalshi_logon, parse_fix,
    pmus_fix_session,
)
from mm.safety.groups import SafeSender
from mm.safety.supervisor import supervisor_once, write_heartbeat
from mm.safety.watchdog import DeadMan, FeedClock
from mm.selector import (
    KalshiMarket, allocate, backtest_kalshi, competition_ratio, defer_polymarket,
    demo_selection, market_from_program, quote_economics, render_report,
)
from mm.types import Side
from mm.venues.kalshi import KalshiAdapter

ROOT = Path(__file__).resolve().parents[1]


def _market(name, *, comp=0.0, incumbent=False, days=3.0, pool=100.0,
            series="KXBRENT", shard_cash=1e9, exchange_index=2,
            other_shard_cash=0.0, other_shard=None, entry_competition=None,
            entry_markout=None, empirical=None, empirical_n=0,
            seconds_left=None, period_seconds=86400.0):
    bids = [(50, comp)] if comp else []
    return KalshiMarket(
        market=name, series=series,
        period_reward_usd=pool, period_seconds=period_seconds,
        seconds_left=period_seconds if seconds_left is None else seconds_left,
        discount_factor=0.5, target_size=100.0,
        yes_bids=list(bids), no_bids=list(bids),
        days_to_settle=days, exchange_index=exchange_index,
        shard_cash_usd=shard_cash, other_shard_cash_usd=other_shard_cash,
        other_shard_index=other_shard, incumbent=incumbent,
        entry_competition=entry_competition, entry_markout_cents=entry_markout,
        empirical_markout_cents=empirical, empirical_n=empirical_n,
    )


def _alloc(markets, bankroll=10_000, chunk=100, max_size=100):
    return allocate(
        markets, bankroll=bankroll, chunk=chunk, max_size=max_size,
        per_market_usd=500, per_series_usd=2_000, per_category_usd=5_000,
    )


class TestKalshiPeriodFloor:
    def test_under_a_dollar_pays_nothing_and_cents_floor_down(self):
        assert kalshi_period_payout(1.0, 0.50) == 0.0
        assert kalshi_period_payout(1.0, 0.30) == 0.0
        assert kalshi_period_payout(1.0, 1.009) == 1.0
        assert kalshi_period_payout(1.0, 0.999) == 0.0
        assert kalshi_period_payout(1.0, 5.0) == 5.0
        assert kalshi_period_payout(1.0, 1.50) == 1.50
        # Three days at $0.50/day clears; three days at $0.10/day does not.
        assert kalshi_period_payout(1.0, 0.50 * 3) == 1.50
        assert kalshi_period_payout(1.0, 0.10 * 3) == 0.0

    def test_the_per_second_accrual_is_not_floored(self):
        params = ProgramParams("M", target_size=100, discount_factor=0.5,
                               period_reward_usd=100, period_seconds=86400)
        tiny = interval_payout_usd(0.01, params, 1.0)
        assert tiny > 0.0
        assert tiny < 1.0


class TestSelector:
    def test_commodity_weekly_beats_a_crowd_and_skips_the_long_event(self):
        thin = _market("KXBRENT-26OCT07", comp=10, pool=50)
        crowded = _market("KXBRENT-26OCT08", comp=5000, pool=50)
        long_event = _market("KXPRES-26NOV01", comp=0, pool=500, days=30,
                             series="KXPRES")
        short_event = _market("KXPRES-26OCT04", comp=100, pool=2, days=10,
                              series="KXPRES")
        picked = _alloc([long_event, crowded, short_event, thin])
        assert [row.market for row in picked.taken] == ["KXBRENT-26OCT07"]
        reasons = dict(picked.excluded)
        assert reasons["KXPRES-26NOV01"].startswith("long_dated_event")
        net, _cap, _share, _y, _n = quote_economics(short_event, 100)
        assert net < 0

    def test_marginal_yield_falls_as_size_grows(self):
        market = _market("KXBRENT-26OCT07", comp=100, pool=100)
        picked = _alloc([market], chunk=100, max_size=200)
        assert len(picked.taken) == 2
        assert picked.taken[1].marginal_per_dollar < picked.taken[0].marginal_per_dollar
        assert picked.taken[1].size == 200

    def test_hysteresis_keeps_a_modest_challenger_out(self):
        incumbent = _market("KXBRENT-26OCT07", comp=100, incumbent=True)
        close = _market("KXBRENT-26OCT08", comp=90)
        far = _market("KXBRENT-26OCT09", comp=20)
        near = _alloc([incumbent, close], bankroll=100)
        assert [row.market for row in near.taken] == ["KXBRENT-26OCT07"]
        displaced = _alloc([incumbent, far], bankroll=100)
        assert [row.market for row in displaced.taken] == ["KXBRENT-26OCT09"]

    def test_competition_spike_and_toxicity_exit(self):
        spiked = _market("KXBRENT-26OCT07", comp=120, incumbent=True,
                         entry_competition=0.5)
        assert competition_ratio(spiked) > 1.0
        toxic = _market("KXBRENT-26OCT08", comp=10, incumbent=True,
                        entry_markout=-0.15, empirical=-1.0, empirical_n=8)
        picked = _alloc([spiked, toxic])
        assert picked.taken == []
        assert dict(picked.exits)["KXBRENT-26OCT07"] == "competition_spike"
        assert dict(picked.exits)["KXBRENT-26OCT08"] == "toxicity"

    def test_unfunded_shard_is_reported_and_not_transferred(self):
        market = _market("KXBRENT-26OCT07", comp=10, pool=50, shard_cash=1,
                         other_shard_cash=500, other_shard=0)
        picked = _alloc([market])
        assert picked.taken == []
        assert ("KXBRENT-26OCT07", "unfunded_shard") in picked.excluded
        assert picked.shard_moves[0].to_shard == 2
        assert picked.shard_moves[0].idle_usd == 500
        assert "not transferred" in picked.shard_moves[0].note

    def test_sub_dollar_period_earns_zero_per_day(self):
        market = _market("KXBRENT-26OCT07", comp=0, pool=0.40)
        net, _cap, share, _y, _n = quote_economics(market, 100)
        assert share == pytest.approx(1.0)
        assert net <= 0
        assert _alloc([market]).taken == []

    def test_no_april_may_calibration_constants(self):
        text = (ROOT / "mm" / "selector.py").read_text(encoding="utf-8")
        for banned in ("207.15", "0.85", "296"):
            assert banned not in text

    def test_polymarket_is_deferred(self):
        assert defer_polymarket("some-slug")[1] == "pm_us_deferred"

    def test_poller_caches_inside_the_interval(self):
        from mm.selector import IncentivePoller
        raw = {
            "id": "p1", "market_ticker": "KXBRENT-26OCT07",
            "period_reward": 5_000_000, "discount_factor_bps": 5000,
            "target_size_fp": "100.00",
            "start_date": "2026-10-01T00:00:00Z",
            "end_date": "2026-10-02T00:00:00Z",
            "paid_out": False,
        }
        poller = IncentivePoller(interval_sec=180)
        first = poller.ingest([raw], now=1_000)
        assert first[0]["period_reward_usd"] == pytest.approx(500)
        richer = dict(raw, period_reward=9_000_000)
        cached = poller.ingest([richer], now=1_010)
        assert cached[0]["period_reward_usd"] == pytest.approx(500)
        refreshed = poller.ingest([richer], now=1_000 + 180)
        assert refreshed[0]["period_reward_usd"] == pytest.approx(900)
        built = market_from_program(refreshed[0], days_to_settle=3, exchange_index=2)
        assert built.series == "KXBRENT"

    def test_backtest_floors_once_per_market(self, tmp_path):
        from mm.recorder import Recorder
        path = tmp_path / "rec.jsonl"
        rec = Recorder(path)
        rec.quote(ts=0, market="KXBRENT-26OCT07", side="yes", price_cents=50,
                  size=100, order_id="y")
        rec.quote(ts=0, market="KXBRENT-26OCT07", side="no", price_cents=50,
                  size=100, order_id="n")
        rec.book(ts=0, market="KXBRENT-26OCT07", yes_bid=50, no_bid=50,
                 yes_size=0, no_size=0)
        rec.book(ts=3600, market="KXBRENT-26OCT07", yes_bid=50, no_bid=50,
                 yes_size=0, no_size=0)
        rec.close()
        params = _market("KXBRENT-26OCT07", pool=100)
        paid = backtest_kalshi(path, {"KXBRENT-26OCT07": params})
        assert paid["KXBRENT-26OCT07"] == pytest.approx(4.16)

        small = tmp_path / "small.jsonl"
        rec = Recorder(small)
        rec.quote(ts=0, market="KXBRENT-26OCT07", side="yes", price_cents=50,
                  size=100, order_id="y")
        rec.quote(ts=0, market="KXBRENT-26OCT07", side="no", price_cents=50,
                  size=100, order_id="n")
        rec.book(ts=0, market="KXBRENT-26OCT07", yes_bid=50, no_bid=50)
        rec.book(ts=3456, market="KXBRENT-26OCT07", yes_bid=50, no_bid=50)
        rec.close()
        paid_small = backtest_kalshi(small, {"KXBRENT-26OCT07": _market(
            "KXBRENT-26OCT07", pool=10)})
        assert paid_small["KXBRENT-26OCT07"] == 0.0

    def test_report_names_the_market(self):
        text = render_report(demo_selection())
        assert "KXBRENT-26OCT07" in text
        assert "net_per_day" in text


class TestArming:
    def test_kalshi_ack_arms_the_quote_manager_only(self, tmp_path):
        import execution.order_request as ore
        from execution.order_request import (
            KALSHI_POST_ONLY_ACK, enable_kalshi_maker_only_enforcement,
        )
        from execution.quote_manager import QuoteManager

        ore.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED = False
        ore.MAKER_ONLY_ENFORCEMENT_VERIFIED = False
        try:
            forced = QuoteManager(paper=False, db_path=str(tmp_path / "forced.db"))
            assert forced.paper is True

            qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
            qm.paper = False
            posted = []

            class Client:
                def post(self, path, body):
                    posted.append(body)
                    return {"order_id": "OID-1"}

            qm.client = Client()
            qm._log_quote_row = MagicMock()
            qm._update_quote_status = MagicMock()
            blocked = qm._place_order("M", "yes", 40, 1, best_opposing_bid_cents=50)
            assert blocked is None
            assert qm.live_blocked == 1
            assert posted == []
            assert ore.MAKER_ONLY_ENFORCEMENT_VERIFIED is False

            enable_kalshi_maker_only_enforcement(KALSHI_POST_ONLY_ACK)
            qm.order_group_for = lambda market: "OG-9"
            placed = qm._place_order("M", "yes", 40, 1, best_opposing_bid_cents=50)
            assert placed is not None
            assert posted[0]["post_only"] is True
            assert posted[0]["order_group_id"] == "OG-9"
            assert ore.MAKER_ONLY_ENFORCEMENT_VERIFIED is False
        finally:
            ore.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED = False
            ore.MAKER_ONLY_ENFORCEMENT_VERIFIED = False


class TestDisconnect:
    def test_fix_cancel_on_disconnect_is_opt_in(self):
        off = kalshi_logon()
        assert parse_fix(off)[8013] == "N"
        acceptor = MockKalshiFixAcceptor()
        acceptor.on_logon(off)
        acceptor.place("a")
        acceptor.drop_socket()
        assert acceptor.orders["a"] == "resting"

        on = kalshi_logon(cancel_on_disconnect=True)
        acceptor.on_logon(on)
        acceptor.place("b")
        acceptor.drop_socket()
        assert acceptor.orders["a"] == "canceled"
        assert acceptor.orders["b"] == "canceled"
        with pytest.raises(Exception):
            kalshi_logon(cancel_on_disconnect=True, listener=True)

        session = pmus_fix_session()
        assert session["CancelOnDisconnect"] == "N"
        gate = MockPMUSFixGateway()
        gate.configure(pmus_fix_session(cancel_on_disconnect=True))
        gate.place("day", tif="DAY")
        gate.place("rest", tif="GTC")
        gate.drop_socket()
        assert gate.orders["day"] == "canceled"
        assert gate.orders["rest"] == "resting"

    def test_safe_sender_groups_on_the_market_shard(self):
        adapter = KalshiAdapter(paper=True)
        sender = SafeSender(adapter)
        resp = sender.place(
            "KXBRENT-26OCT07", Side.YES, 40, 5,
            exchange_index=2, best_opposing_bid_cents=50,
        )
        assert resp["ok"]
        assert resp["body"]["order_group_id"]
        assert resp["body"]["exchange_index"] == 2
        create = adapter.sent[0]
        assert create["path"] == "/portfolio/order_groups/create"
        assert create["body"]["exchange_index"] == 2
        direct = KalshiAdapter(paper=True)
        direct.place("KXTEST-1", Side.NO, 40, 10, best_opposing_bid_cents=50)
        assert direct.sent[0]["path"] == "/portfolio/events/orders"

        live = KalshiAdapter(paper=False)
        refused = SafeSender(live).place(
            "KXBRENT-26OCT07", Side.YES, 40, 5,
            exchange_index=2, best_opposing_bid_cents=50,
        )
        assert refused["ok"] is False
        assert "live_blocked" in refused["error"]

    def test_dead_man_triggers_the_group_once(self):
        adapter = KalshiAdapter(paper=True)
        sender = SafeSender(adapter)
        sender.place(
            "KXBRENT-26OCT07", Side.YES, 40, 5,
            exchange_index=2, best_opposing_bid_cents=50,
        )
        reasons = []

        def cancel(reason):
            reasons.append(reason)
            sender.trigger_all()

        clock = FeedClock()
        clock.beat_ws(0)
        clock.beat_rest(0)
        dead = DeadMan(clock, cancel, stale_ms=1000)
        assert dead.check(0) == ""
        assert dead.check(5) == "market_data_stale"
        assert dead.check(9) == "market_data_stale"
        assert reasons == ["market_data_stale"]
        triggers = [row for row in adapter.sent if row["method"] == "PUT"]
        assert len(triggers) == 1
        assert triggers[0]["path"].endswith("/trigger")
        assert triggers[0]["params"]["exchange_index"] == 2

        dropped = FeedClock()
        dropped.beat_ws(10)
        dropped.drop()
        seen = []
        man = DeadMan(dropped, seen.append, stale_ms=3000)
        assert man.check(10) == "socket_dropped"

        from mm.risk import RiskEngine
        risk = RiskEngine()
        risk.killed = True
        fresh = FeedClock()
        fresh.beat_ws(10)
        fresh.beat_rest(10)
        killed = []
        man = DeadMan(fresh, killed.append, risk=risk)
        assert man.check(10) == "risk_kill"
        assert killed == ["risk_kill"]

    def test_supervisor_cancels_when_the_loop_stops(self, tmp_path):
        heartbeat = tmp_path / "hb"
        cancel_log = tmp_path / "cancel"
        write_heartbeat(heartbeat, now=1_000)
        assert supervisor_once(heartbeat, cancel_log, stale_ms=1000, now=1_000.5) == 0
        assert not cancel_log.exists()
        assert supervisor_once(heartbeat, cancel_log, stale_ms=1000, now=1_005) == 2
        assert cancel_log.read_text(encoding="utf-8").strip() == "cancel_all"

        missing = tmp_path / "gone"
        log2 = tmp_path / "cancel2"
        assert supervisor_once(missing, log2, stale_ms=1000, now=0) == 2

        proc = subprocess.run(
            [sys.executable, "-m", "mm.safety.supervisor",
             "--heartbeat", str(heartbeat),
             "--cancel-log", str(tmp_path / "from-proc"),
             "--stale-ms", "1000", "--once"],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 2
        assert "cancel_all" in (tmp_path / "from-proc").read_text(encoding="utf-8")
