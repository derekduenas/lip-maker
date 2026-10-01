"""In-loop adverse-selection guard (2026-09-30).

Unit tests pin the guard's arithmetic; integration tests prove the runner
actually PULLS quotes (instead of freezing them) and that the inventory cap
keeps the reducing side alive while removing the side that adds exposure.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine.adverse_selection import (
    AdverseSelectionGuard, ASConfig, inventory_side_controls, markout_cents,
    yes_mid_cents,
)
from execution.kalshi_ws import BookLevel, BookState, FillEvent
from execution.quote_manager import InventoryState, QuoteManager, QuoteTarget, RestingOrder
from run_paper import FORCE_PULL_REASONS, PaperRunner, TRANSIENT_SKIP_REASONS

TKR = "KXTEST-26SEP30-T1"


# ── pure arithmetic ───────────────────────────────────────────────────────

class TestMarkoutArithmetic:
    def test_yes_fill_marked_against_yes_mid(self):
        assert markout_cents("yes", 50, 47) == -3
        assert markout_cents("yes", 50, 52) == 2

    def test_no_fill_marked_against_complement(self):
        # bought NO at 48; YES mid rises to 55 ⇒ NO worth 45 ⇒ -3c
        assert markout_cents("no", 48, 55) == -3

    def test_bad_side_raises(self):
        with pytest.raises(ValueError):
            markout_cents("ask", 50, 50)

    def test_yes_mid_from_two_bid_ladders(self):
        assert yes_mid_cents(48, 50) == 49.0       # ask = 100-50 = 50
        assert yes_mid_cents(None, 50) is None
        assert yes_mid_cents(60, 50) is None       # crossed: refuse


class TestGuard:
    def _g(self, **kw):
        return AdverseSelectionGuard(ASConfig(**kw))

    def test_clear_by_default(self):
        assert self._g().decide(TKR, 1000.0).is_clear

    def test_post_fill_fade_suppresses_only_filled_side_then_expires(self):
        g = self._g(fill_cooldown_sec=15)
        g.record_fill(TKR, "yes", 50, 10, ts=1000.0)
        d = g.decide(TKR, 1005.0)
        assert d.suppress == frozenset({"yes"}) and not d.pull_market
        assert g.decide(TKR, 1016.0).is_clear

    def test_fill_burst_pulls_whole_market(self):
        g = self._g(burst_contracts=100, burst_window_sec=60, burst_cooldown_sec=120)
        g.record_fill(TKR, "yes", 50, 60, ts=1000.0)
        assert not g.decide(TKR, 1001.0).pull_market
        g.record_fill(TKR, "yes", 49, 45, ts=1030.0)
        d = g.decide(TKR, 1031.0)
        assert d.pull_market and "fill_burst" in d.reason
        assert g.decide(TKR, 1149.0).pull_market
        assert not g.decide(TKR, 1151.0).pull_market

    def test_burst_counts_same_side_only_within_window(self):
        g = self._g(burst_contracts=100, burst_window_sec=60)
        g.record_fill(TKR, "yes", 50, 60, ts=1000.0)
        g.record_fill(TKR, "no", 50, 60, ts=1001.0)       # other side
        g.record_fill(TKR, "yes", 50, 60, ts=1100.0)      # outside window
        assert not g.decide(TKR, 1101.0).pull_market

    def _toxic_fills(self, g, n, start=1000.0, drift=-4):
        t = start
        for i in range(n):
            g.record_mid(TKR, 50, t)
            g.record_fill(TKR, "yes", 50, 5, ts=t)
            g.record_mid(TKR, 50 + drift, t + 1)
            g.record_mid(TKR, 50 + drift, t + 31)
            t += 40
        return t

    def test_markout_toxicity_pulls_after_min_obs(self):
        g = self._g(min_obs=3, pull_markout_cents=3.0, toxic_cooldown_sec=600,
                    fill_cooldown_sec=0, burst_contracts=1e9)
        t = self._toxic_fills(g, 2)
        assert not g.decide(TKR, t).pull_market            # only 2 obs
        t = self._toxic_fills(g, 1, start=t)
        d = g.decide(TKR, t)
        assert d.pull_market and "toxic_markout" in d.reason
        assert g.decide(TKR, t + 599).pull_market
        # after cooldown the label must be re-earned
        assert g.decide(TKR, t + 601).is_clear

    def test_mild_toxicity_widens_one_tick(self):
        g = self._g(min_obs=3, widen_markout_cents=1.0, pull_markout_cents=3.0,
                    fill_cooldown_sec=0, burst_contracts=1e9)
        t = self._toxic_fills(g, 3, drift=-2)
        d = g.decide(TKR, t)
        assert not d.pull_market and d.tick_back == 1

    def test_benign_flow_stays_clear(self):
        g = self._g(min_obs=3, fill_cooldown_sec=0, burst_contracts=1e9)
        t = self._toxic_fills(g, 5, drift=+1)
        assert g.decide(TKR, t).is_clear

    def test_unmarkable_when_no_recent_mid(self):
        g = self._g(max_mid_gap_sec=2, min_obs=1, fill_cooldown_sec=0)
        g.record_fill(TKR, "yes", 50, 5, ts=1000.0)
        g.record_mid(TKR, 40, 1100.0)       # far beyond every horizon, no mid near
        assert g.counters["markout_unmarkable"] >= 1
        assert g.toxicity(TKR) == (None, 0)

    def test_volatility_note_starts_cooldown(self):
        g = self._g(volatility_cooldown_sec=30)
        g.note_volatility(TKR, 1000.0)
        assert g.decide(TKR, 1010.0).pull_market
        assert not g.decide(TKR, 1031.0).pull_market

    def test_markouts_persisted(self, tmp_path):
        db = str(tmp_path / "as.db")
        g = AdverseSelectionGuard(ASConfig(fill_cooldown_sec=0), db_path=db)
        g.record_mid(TKR, 50, 1000.0)
        g.record_fill(TKR, "no", 50, 3, ts=1000.0)
        g.record_mid(TKR, 53, 1004.0)
        g.record_mid(TKR, 53, 1006.0)       # triggers maturity of the 5s horizon
        rows = sqlite3.connect(db).execute(
            "SELECT side, horizon_sec, markout_cents FROM as_markouts").fetchall()
        assert rows == [("no", 5.0, pytest.approx(-3.0))]

    def test_unpriced_fill_still_counts_for_burst(self):
        g = self._g(burst_contracts=10)
        g.record_fill(TKR, "yes", None, 12, ts=1000.0)
        assert g.decide(TKR, 1001.0).pull_market


class TestInventorySideControls:
    def test_flat(self):
        assert inventory_side_controls(0, max_net_contracts=100) == (None, 0, "")

    def test_soft_tick_back(self):
        s, back, why = inventory_side_controls(60, max_net_contracts=100)
        assert s is None and back == 1 and why.startswith("inv_soft:yes")

    def test_hard_cap_suppresses_heavy_side(self):
        assert inventory_side_controls(100, max_net_contracts=100)[0] == "yes"
        assert inventory_side_controls(-150, max_net_contracts=100)[0] == "no"


# ── runner integration ────────────────────────────────────────────────────

def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "as_runner.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(settings, "DB_PATH", str(path))
    return str(path)


def _market(**kw):
    m = dict(market_ticker=TKR, target_size=50, discount_factor=0.5,
             reward_per_day_usd=100.0, period_reward_usd=700.0, period_seconds=7 * 86400.0,
             start_date="2026-01-01T00:00:00Z", end_date="2028-01-01T00:00:00Z")
    m.update(kw)
    return m


@pytest.fixture
def runner(db):
    r = PaperRunner([_market()])
    r.qm.paper = True
    r.qm.resting = {}
    r.qm.cancel_all = MagicMock(return_value=1)
    r._refresh_blacklist = MagicMock()
    r._is_blacklisted = MagicMock(return_value=False)
    r.qm.reconcile = MagicMock(return_value={"action": "ok"})
    r.qm._refresh_inventory = MagicMock()
    r.qm.inventory = {}
    r.last_complete_scan_ts = time.time()
    from engine.market_clock import MarketClock
    now = time.time()
    r.market_clock = MarketClock(fetcher=lambda t: {
        "open_time": _iso(now - 3600), "close_time": _iso(now + 6 * 3600)})
    r._economic_choice = MagicMock(return_value=None)   # isolate from economics
    return r


def _book(yes, no):
    b = BookState(market_ticker=TKR)
    b.yes_bids = sorted([BookLevel(p, float(s)) for p, s in yes], key=lambda l: -l.price_cents)
    b.no_bids = sorted([BookLevel(p, float(s)) for p, s in no], key=lambda l: -l.price_cents)
    b.snapshot_count = 1
    return b


def _order(side, price, size):
    return RestingOrder(order_id=f"{side}-{price}", market_ticker=TKR, side=side,
                        price_cents=price, size_contracts=float(size), placed_at=time.time(),
                        paper=True, client_order_id="LIP-x")


BOOK = dict(yes=[(48, 200)], no=[(50, 200)])


class TestRunnerIntegration:
    def test_reason_sets(self):
        assert "volatility_pull" not in TRANSIENT_SKIP_REASONS
        assert {"as_pull", "volatility_pull"} <= FORCE_PULL_REASONS

    def test_baseline_quotes_both_sides(self, runner):
        t = runner._quote_target_for(_book(**BOOK))
        assert t is not None and t.yes_bid_cents == 48 and t.no_bid_cents == 50

    def test_volatility_now_pulls_instead_of_freezing(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "PULL_ON_VOLATILITY", True)
        runner._is_volatile = MagicMock(return_value=True)
        assert runner._quote_target_for(_book(**BOOK)) is None
        assert runner._skip_reason[TKR] == "volatility_pull"
        # and the cooldown outlives the volatile tick
        runner._is_volatile = MagicMock(return_value=False)
        assert runner._quote_target_for(_book(**BOOK)) is None
        assert runner._skip_reason[TKR].startswith("as_pull")

    def test_volatility_legacy_behaviour_behind_flag(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "PULL_ON_VOLATILITY", False)
        runner._is_volatile = MagicMock(return_value=True)
        assert runner._quote_target_for(_book(**BOOK)) is None
        assert runner._skip_reason[TKR] == "volatility"

    def test_volatility_pull_cancels_immediately_despite_throttle(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "PULL_ON_VOLATILITY", True)
        runner.qm.resting[TKR] = [_order("yes", 48, 30), _order("no", 50, 30)]
        runner._skip_cancel_ts[TKR] = time.time()          # throttle would block
        runner._is_volatile = MagicMock(return_value=True)
        runner.last_score_ts[TKR] = 0
        asyncio.run(runner.on_book_update(_book(**BOOK)))
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)
        runner.qm.reconcile.assert_not_called()

    def test_fill_fades_filled_side_only(self, runner):
        runner.as_guard.record_fill(TKR, "yes", 48, 5, ts=time.time())
        t = runner._quote_target_for(_book(**BOOK))
        assert t is not None and t.yes_bid_cents is None and t.no_bid_cents == 50

    def test_on_fill_feeds_guard(self, runner):
        runner.qm.apply_fill = MagicMock(return_value=None)
        runner.qm.last_fill_status = "applied"
        runner._actually_resting = MagicMock(return_value=False)
        ev = FillEvent(order_id="o1", market_ticker=TKR, side="no", count=4.0,
                       trade_id="", price_cents_exact=50.0, is_taker=False,
                       ts=time.time())
        runner.on_fill(ev)
        assert runner.as_guard.counters["fills"] == 1
        assert runner.as_guard.decide(TKR, time.time()).suppress == frozenset({"no"})

    def test_inventory_hard_cap_keeps_reducing_side(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "MAX_NET_INVENTORY_USD", 50.0)   # 100 contracts
        runner.qm.inventory[TKR] = InventoryState(TKR, net_yes_contracts=120)
        t = runner._quote_target_for(_book(**BOOK))
        assert t is not None
        assert t.yes_bid_cents is None            # heavy side removed
        assert t.no_bid_cents == 50               # reducing side still quoted

    def test_inventory_soft_zone_backs_heavy_side_off(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "MAX_NET_INVENTORY_USD", 50.0)
        runner.qm.inventory[TKR] = InventoryState(TKR, net_yes_contracts=-60)  # long NO
        thin = _book(yes=[(48, 200)], no=[(50, 30), (49, 40)])   # cutoff at 49
        t = runner._quote_target_for(thin)
        assert t is not None and t.yes_bid_cents == 48 and t.no_bid_cents == 49

    def test_inventory_soft_zone_falls_back_when_tick_back_would_not_score(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "MAX_NET_INVENTORY_USD", 50.0)
        runner.qm.inventory[TKR] = InventoryState(TKR, net_yes_contracts=-60)
        t = runner._quote_target_for(_book(**BOOK))   # 200 deep at the NO touch
        assert t is not None and t.no_bid_cents == 50  # back at best, size-skew only

    def test_guard_disabled_is_legacy(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "AS_GUARD_ENABLED", False)
        runner.as_guard.note_volatility(TKR, time.time())
        t = runner._quote_target_for(_book(**BOOK))
        assert t is not None and t.yes_bid_cents == 48


class TestSafetyGateSideAware:
    def _qm(self, tmp_path, net):
        qm = QuoteManager(paper=True, db_path=str(tmp_path / "qm.db"))
        qm._refresh_inventory = lambda t: None
        qm.inventory[TKR] = InventoryState(TKR, net_yes_contracts=net)
        qm._get_balance = lambda: 10_000.0
        return qm

    def test_reducing_only_target_passes_net_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "MAX_NET_INVENTORY_USD", 50.0)
        monkeypatch.setattr(settings, "INVENTORY_SIDE_CAP_ENABLED", True)
        import risk.sentinel as sent
        monkeypatch.setattr(sent.Sentinel, "approve", lambda self, t: (True, "ok"))
        import engine.series_ev as sev
        monkeypatch.setattr(sev, "check_series_ev", lambda *a, **k: (True, "ok"))
        qm = self._qm(tmp_path, 200)
        ok, why = qm._passes_safety(QuoteTarget(TKR, None, 50, 30))
        assert ok, why
        ok, why = qm._passes_safety(QuoteTarget(TKR, 48, 50, 30))
        assert not ok and why.startswith("net_inv")

    def test_flag_off_is_legacy_refusal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "MAX_NET_INVENTORY_USD", 50.0)
        monkeypatch.setattr(settings, "INVENTORY_SIDE_CAP_ENABLED", False)
        import risk.sentinel as sent
        monkeypatch.setattr(sent.Sentinel, "approve", lambda self, t: (True, "ok"))
        import engine.series_ev as sev
        monkeypatch.setattr(sev, "check_series_ev", lambda *a, **k: (True, "ok"))
        qm = self._qm(tmp_path, 200)
        ok, why = qm._passes_safety(QuoteTarget(TKR, None, 50, 30))
        assert not ok and why.startswith("net_inv")
