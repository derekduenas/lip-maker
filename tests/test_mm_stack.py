"""Tests for the mm package: one class per module."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from mm.accounting import (
    Books, kalshi_fee_usd, pm_us_maker_rebate_usd, pm_us_taker_fee_usd,
)
from mm.bankroll import capital_usd
from mm.diff import plan_resting
from mm.fair_value import (
    Reference, clear_references, digital_yes_cents, fair_yes, family_for_series,
    register_reference,
)
from mm.hedge import Equivalence, HedgeManager
from mm.order_machine import IllegalTransition, OrderBook
from mm.pool import Pool, select
from mm.recorder import Recorder
from mm.replay import replay
from mm.report import render_daily
from mm.reservation import apply_skew, quote_reservation
from mm.risk import FILL_CLOCK, FillClock, Limits, RiskEngine, order_group_contracts_limit
from mm.types import ManagedOrder, OrderState, Side, VenueName, VenueOrderView
from mm.venues.forecastex import ForecastExAdapter, get_adapter
from mm.venues.kalshi import KalshiAdapter
from mm.venues.pmus import PMUSAdapter


class TestBankroll:
    def test_default_is_the_ledger_figure(self, monkeypatch):
        monkeypatch.delenv("LIP_BANKROLL", raising=False)
        monkeypatch.delenv("LIP_ACCOUNT_USD", raising=False)
        assert capital_usd() == Decimal("5000")

    def test_bankroll_env_wins_over_account_env(self, monkeypatch):
        monkeypatch.setenv("LIP_BANKROLL", "750")
        monkeypatch.setenv("LIP_ACCOUNT_USD", "5000")
        assert capital_usd() == Decimal("750")

    def test_settings_read_the_same_number(self, monkeypatch):
        monkeypatch.delenv("LIP_BANKROLL", raising=False)
        monkeypatch.delenv("LIP_ACCOUNT_USD", raising=False)
        from config import settings
        # settings captured the value at import. Both names are one assignment.
        assert settings.BANKROLL_USD == settings.ACCOUNT_OPENING_CASH_USD


class TestDiff:
    def test_size_down_keeps_queue(self):
        d = plan_resting(40, 25, 40, 10)
        assert d.action == "decrease" and d.queue_preserved

    def test_price_change_amends_and_loses_queue(self):
        d = plan_resting(40, 25, 42, 25)
        assert d.action == "amend" and not d.queue_preserved

    def test_fade_does_not_top_up(self):
        d = plan_resting(40, 10, 40, 25, fade=True)
        assert d.action == "keep" and d.reason == "fade_no_topup"

    def test_pull_cancels(self):
        assert plan_resting(40, 10, None, 0).action == "cancel"


class TestOrderMachine:
    def test_illegal_transition_raises(self):
        book = OrderBook()
        book.add(ManagedOrder("c1", VenueName.KALSHI, "M", Side.YES, 40, 10,
                              state=OrderState.PENDING_NEW))
        book.transition("c1", OrderState.FILLED, ts=1.0)
        with pytest.raises(IllegalTransition):
            book.transition("c1", OrderState.RESTING, ts=2.0)

    def test_reconcile_venue_wins_on_size_and_adopts_unknown(self):
        book = OrderBook()
        book.add(ManagedOrder("c1", VenueName.KALSHI, "M", Side.YES, 40, 100,
                              state=OrderState.RESTING, order_id="v1"))
        report = book.reconcile([
            VenueOrderView("v1", "c1", "M", Side.YES, 40, 25.5, "resting"),
            VenueOrderView("v2", "c2", "M", Side.NO, 55, 8, "resting"),
        ], ts=3.0)
        assert book.get("c1").remaining == 25.5
        assert "c1" in report.overwritten
        assert any(book.get(c).order_id == "v2" for c in report.adopted)

    def test_missing_resting_order_is_cancelled(self):
        book = OrderBook()
        book.add(ManagedOrder("c1", VenueName.KALSHI, "M", Side.YES, 40, 10,
                              state=OrderState.RESTING, order_id="gone"))
        report = book.reconcile([], ts=4.0)
        assert book.get("c1").state == OrderState.CANCELLED
        assert report.gone == ["c1"]


class TestVenues:
    def test_kalshi_no_buy_is_a_yes_ask(self):
        a = KalshiAdapter(paper=True)
        resp = a.place("KXTEST-1", Side.NO, 40, 10, best_opposing_bid_cents=50)
        assert resp["ok"]
        body = resp["body"]
        assert body["side"] == "ask" and body["price"] == "0.6000"
        assert body["post_only"] is True
        assert body["count"] == "10.00"
        assert a.sent[0]["path"] == "/portfolio/events/orders"

    def test_kalshi_decrease_keeps_queue_flag(self):
        a = KalshiAdapter(paper=True)
        resp = a.decrease("oid", 8, market="KXTEST-1")
        assert resp["queue_preserved"] is True
        assert a.sent[0]["body"]["reduce_to"] == "8.00"
        assert a.sent[0]["body"]["market_ticker"] == "KXTEST-1"
        assert a.sent[0]["path"].endswith("/decrease")

    def test_kalshi_live_place_is_blocked(self):
        a = KalshiAdapter(paper=False)
        resp = a.place("KXTEST-1", Side.YES, 40, 10, best_opposing_bid_cents=50)
        assert resp["ok"] is False and "live_blocked" in resp["error"]

    def test_order_group_clamped(self):
        assert order_group_contracts_limit(0) == 1
        assert order_group_contracts_limit(2_000_000) == 1_000_000
        a = KalshiAdapter(paper=True)
        spec = a.create_order_group(30)
        assert spec["ok"] and spec["contracts_limit"] == 30

    def test_pmus_post_only_and_modify_and_cancel_all(self):
        a = PMUSAdapter(paper=True)
        placed = a.place("some-market", intent="ORDER_INTENT_BUY_LONG",
                         price_cents=49, quantity=10, now=1.0)
        assert placed["body"]["participateDontInitiate"] is True
        mod = a.modify(placed["order_id"], price_cents=48, quantity=10, now=1.1)
        assert mod["queue_preserved"] is False
        assert mod["body"]["participateDontInitiate"] is True
        cancelled = a.cancel_all(["some-market"], now=1.2)
        assert cancelled["ok"] and a.sent[-1]["path"] == "/v1/orders/open/cancel"

    def test_pmus_rate_budget(self):
        a = PMUSAdapter(paper=True)
        ok = 0
        for i in range(25):
            if a.place("m", intent="ORDER_INTENT_BUY_LONG", price_cents=40,
                       quantity=1, now=0.0).get("ok"):
                ok += 1
        assert ok == 20

    def test_forecastex_is_pluggable_and_refuses(self):
        a = get_adapter("forecastex")
        assert isinstance(a, ForecastExAdapter)
        assert a.place()["error"] == "not_available"

    def test_parse_yes_ask_as_no_bid(self):
        view = KalshiAdapter.parse_resting({
            "order_id": "v", "client_order_id": "c", "ticker": "M",
            "side": "ask", "price": "0.6000", "remaining_count": "4.00",
            "status": "resting",
        })
        assert view.side == Side.NO and view.price_cents == 40


class TestFairAndReservation:
    def test_family_and_digital(self):
        assert family_for_series("KXBRENTD") == "commodity"
        assert family_for_series("KXHIGHLAX") == "weather"
        assert family_for_series("KXTEST") == "event"
        assert digital_yes_cents(100, 100, 5) == pytest.approx(50.0)

    def test_external_jump_pulls(self):
        ref = Reference("BZ", spot=110, prev_spot=100, strike=100, sigma_horizon=1)
        fair = fair_yes("KXBRENTD-1", 50, ref)
        assert fair.pull and fair.pull_reason.startswith("external_move")

    def test_weather_observation_gap_pulls(self):
        ref = Reference("NWS", spot=0, prev_spot=None, strike=None,
                        has_observation=True, observation_gap=3.0)
        fair = fair_yes("KXHIGHLAX-1", 40, ref)
        assert fair.pull

    def test_reservation_moves_price_not_only_size(self):
        # Full long, σ = 5¢ per √hour, τ = 1h, γ = 0.04 → reservation 1¢ under fair.
        res = quote_reservation(50, net_yes=100, cap_contracts=100,
                                sigma_cents=5, tau_hours=1)
        assert res.skew_cents == -1
        assert res.suppress_side == "yes"
        yes, no = apply_skew(48, 50, res.skew_cents)
        assert (yes, no) == (47, 51)

    def test_runner_hook_is_idle_without_a_reference(self):
        clear_references()
        from mm.fair_value import lookup_reference
        assert lookup_reference("KXTEST-26SEP30-T1") is None


class TestRisk:
    def test_small_live_caps(self):
        limits = Limits.from_capital(Decimal("800"))
        assert limits.daily_loss_usd == Decimal("40")
        assert limits.per_market_usd == Decimal("50")
        assert limits.per_underlying_usd == Decimal("150")

    def test_underlying_cap_stacks_brent(self):
        # $5,000 book: $500 per market, $1,250 per commodity family.
        # Three brent contracts under the market cap still share one underlying.
        eng = RiskEngine(limits=Limits.from_capital(Decimal("5000")),
                         clock=FillClock())
        eng.commit("KXBRENTD-A", "kalshi", Decimal("450"))
        eng.commit("KXBRENTD-B", "kalshi", Decimal("450"))
        d = eng.check_quote(market="KXBRENTW-C", venue="kalshi", add_usd=Decimal("450"))
        assert not d.allowed and "per_underlying" in d.reason

    def test_unrelated_events_do_not_share_an_underlying(self):
        eng = RiskEngine(limits=Limits.from_capital(Decimal("5000")),
                         clock=FillClock())
        eng.commit("KXTEST-A", "kalshi", Decimal("400"))
        d = eng.check_quote(market="KXOTHER-B", venue="kalshi", add_usd=Decimal("400"))
        assert d.allowed

    def test_fill_clock_latches(self):
        clock = FillClock(limit=3)
        clock.record(3, now=1000.0)
        ok, reason = clock.check(now=10_000.0)   # long after the window
        assert not ok and "fills_per_minute" in reason
        clock.reset()
        assert clock.check(now=10_000.0)[0]

    def test_daily_loss_and_disconnect_cancel_all(self):
        eng = RiskEngine(limits=Limits.from_capital(Decimal("800")),
                         clock=FillClock())
        d = eng.note_daily_pnl(Decimal("-40"))
        assert d.cancel_all and not d.allowed
        eng2 = RiskEngine(limits=Limits.from_capital(Decimal("800")),
                          clock=FillClock())
        assert eng2.on_disconnect(3.0).cancel_all
        assert not eng2.on_disconnect(1.0).cancel_all or eng2.killed

    def test_constitution_clock_is_what_sentinel_reads(self):
        FILL_CLOCK.reset()
        try:
            from risk.sentinel import Sentinel
            ok, _ = Sentinel(db_path=":memory:")._check_fill_halt()
            assert ok
            FILL_CLOCK.record(FILL_CLOCK.limit, now=1.0)
            ok, reason = Sentinel(db_path=":memory:")._check_fill_halt()
            assert not ok and "fills_per_minute" in reason
        finally:
            FILL_CLOCK.reset()


class TestHedge:
    def test_unlisted_pair_unwinds_passively_and_logs(self):
        h = HedgeManager(soft_cap_contracts=10)
        d = h.decide(market="KXBRENTD-1", net_yes=40, toxic=True, fair_cents=50,
                     hedge_price_cents=55, taker_fee_usd=Decimal("1"),
                     expected_loss_usd=Decimal("10"), pmus_slug="brent-daily")
        assert d.action == "unwind_passive" and d.paper
        assert "HEDGE paper" in d.log_line and h.decisions[-1] is d

    def test_equivalent_pair_simulates_when_the_hedge_is_cheaper(self):
        h = HedgeManager(soft_cap_contracts=10)
        h.allow(Equivalence("KXBRENTD-1", "brent-daily", True, True, True, True))
        d = h.decide(market="KXBRENTD-1", net_yes=20, toxic=True, fair_cents=50,
                     hedge_price_cents=51, taker_fee_usd=Decimal("0.10"),
                     expected_loss_usd=Decimal("5"), pmus_slug="brent-daily")
        assert d.action == "hedge_simulated"
        assert d.simulated_price_cents == 51

    def test_denied_equivalence_does_not_hedge(self):
        h = HedgeManager(soft_cap_contracts=10)
        h.allow(Equivalence("KXBRENTD-1", "brent-daily", True, True, True, False))
        d = h.decide(market="KXBRENTD-1", net_yes=20, toxic=True, fair_cents=50,
                     hedge_price_cents=51, taker_fee_usd=Decimal("0.10"),
                     expected_loss_usd=Decimal("5"), pmus_slug="brent-daily")
        assert d.action == "unwind_passive"


class TestPool:
    def _pool(self, market, series, days, reward, markout, capital=Decimal("100"),
              ref=False, obs=False):
        return Pool(market, "kalshi", series, days, Decimal(reward), Decimal("10"),
                    Decimal(markout), Decimal("0"), Decimal("0"), capital,
                    has_reference=ref, has_observation=obs)

    def test_long_dated_event_excluded_reference_preferred_on_a_tie(self):
        event = self._pool("KXGOV-1", "KXGOV", 100, "5", "0")
        near_event = self._pool("KXSHOW-1", "KXSHOW", 3, "2", "0")
        brent = self._pool("KXBRENTD-1", "KXBRENTD", 3, "2", "0", ref=True)
        chosen = select([event, near_event, brent])
        assert event.market not in [c.pool.market for c in chosen]
        assert chosen[0].pool.market == "KXBRENTD-1"

    def test_toxic_reference_loses_to_a_clean_book(self):
        toxic = self._pool("KXBRENTD-1", "KXBRENTD", 2, "0.10", "-50", ref=True)
        clean = self._pool("KXSHOW-1", "KXSHOW", 2, "5", "0")
        assert select([toxic, clean])[0].pool.market == "KXSHOW-1"

    def test_unknown_settlement_excluded(self):
        p = self._pool("KXTEST-1", "KXTEST", None, "9", "0")
        assert select([p]) == []


class TestAccounting:
    def test_quadratic_maker_is_free_and_combo_is_not(self):
        assert kalshi_fee_usd(50, 100, fee_type="quadratic") == 0
        # 0.035 × 100 × 0.25 = 0.875
        assert kalshi_fee_usd(50, 100, fee_type="quadratic_with_combo_maker_fees") == Decimal("0.875")

    def test_pm_us_example_from_the_fee_page(self):
        # docs: 0.0695 × 1000 × 0.10 × 0.90 = $6.255 → $6.26 banker's
        #        0.0125 × 1000 × 0.10 × 0.90 = $1.125 → $1.12
        assert pm_us_taker_fee_usd(10, 1000) == Decimal("6.26")
        assert pm_us_maker_rebate_usd(10, 1000) == Decimal("1.12")

    def test_estimate_is_not_cash_and_paid_needs_a_source(self):
        books = Books()
        b = books.book("M")
        b.add_estimate(Decimal("3"))
        b.realized_usd = Decimal("-1")
        assert b.cash_pnl_usd == Decimal("-1")
        with pytest.raises(ValueError):
            b.add_paid(Decimal("3"), "model")
        b.add_paid(Decimal("2"), "kalshi_api")
        assert b.cash_pnl_usd == Decimal("1")
        assert books.estimated_total() == Decimal("3")


class TestReplay:
    def test_recorded_book_reproduces_paper_pnl(self, tmp_path: Path):
        ts = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
        path = tmp_path / "rec.jsonl"
        rec = Recorder(path)
        rec.book(ts=ts, market="KXBRENTD-1", yes_bid=40, no_bid=58, yes_size=0, no_size=0)
        rec.quote(ts=ts, market="KXBRENTD-1", side="yes", price_cents=40,
                  size=10, order_id="p1")
        rec.trade(trade={
            "trade_id": "t1", "ticker": "KXBRENTD-1", "count_fp": "10.00",
            "yes_price_dollars": "0.40", "no_price_dollars": "0.60",
            "taker_side": "no", "created_time": "2026-10-01T00:00:05+00:00",
        })
        rec.write("settlement", market="KXBRENTD-1", yes_cents=100)
        rec.write("estimate", market="KXBRENTD-1", usd="1.50")
        rec.close()
        result = replay(path, fee_type="quadratic")
        assert len(result.fills) == 1
        # 10 contracts bought at 40¢, settle at 100¢ → $6. No maker fee.
        assert result.cash_pnl_usd == Decimal("6")
        assert result.estimated_reward_usd == Decimal("1.50")
        text = render_daily(result, day="2026-10-01")
        assert "cash_pnl_usd 6.0000" in text
        assert "estimated_reward_usd 1.5000" in text
        assert "estimates excluded" in text
