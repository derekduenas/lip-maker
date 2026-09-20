"""Audit #3/#7 (2026-09-20): score ACTUAL resting orders, no live
double-count, real payout units, and skipped targets never leave exposure.

Covers:
  - regression: target at 50, real order at 49, competing depth fills the
    cutoff at 50 ⇒ our credit is ZERO (old code scored the target ⇒ >0)
  - live mode scores the public book as-is (our orders already inside it);
    paper mode augments (shadow orders never reach the venue)
  - hypothetical scoring respects per-side size overrides
  - one-sided / sub-min resting ⇒ was_resting=0, share=0
  - estimated_payout_usd = share × pool/period_seconds × elapsed, bounded,
    zero when not qualified; first row claims nothing
  - _program_params_from_market: pool+period preferred, per-day fallback
  - skip reasons: transient keeps resting; non-transient cancels (throttled)
  - heartbeat and book paths share _score_market
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine.lip_scorer import ProgramParams, interval_payout_usd, snapshot_share
from execution.kalshi_ws import BookLevel, BookState
from execution.quote_manager import QuoteTarget, RestingOrder
import run_paper as rp
from run_paper import PaperRunner, TRANSIENT_SKIP_REASONS, _program_params_from_market

TKR = "KXTEST-26SEP30-T1"
MIN = settings.MIN_QUOTE_SIZE_CONTRACTS


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "snap.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE lip_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_ticker TEXT, captured_at TEXT,
            our_score REAL, total_score REAL, yes_qualified INTEGER, no_qualified INTEGER,
            snapshot_valid INTEGER, estimated_payout_usd REAL
        );
    """)  # legacy shape: was_resting / our_share must be added by the runner
    conn.commit(); conn.close()
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
    r.qm.cancel_all = MagicMock()
    r._refresh_blacklist = MagicMock()
    r._is_blacklisted = MagicMock(return_value=False)
    r.qm.reconcile = MagicMock(return_value={"action": "ok"})
    r.last_complete_scan_ts = time.time()      # freshness gate satisfied
    return r


def _order(side, price, size, oid=None):
    return RestingOrder(order_id=oid or f"{side}-{price}", market_ticker=TKR, side=side,
                        price_cents=price, size_contracts=size, placed_at=time.time(), paper=True)


def _book(yes, no):
    b = BookState(market_ticker=TKR)
    b.yes_bids = sorted([BookLevel(p, float(s)) for p, s in yes], key=lambda l: -l.price_cents)
    b.no_bids = sorted([BookLevel(p, float(s)) for p, s in no], key=lambda l: -l.price_cents)
    b.snapshot_count = 1
    return b


def _target(yes=50, no=50, size=MIN, **kw):
    return QuoteTarget(market_ticker=TKR, yes_bid_cents=yes, no_bid_cents=no,
                       size_contracts=size, **kw)


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT our_score, total_score, yes_qualified, no_qualified, "
                            "snapshot_valid, estimated_payout_usd, was_resting, our_share "
                            "FROM lip_snapshots ORDER BY id").fetchall()
    finally:
        conn.close()


# ── Scoring what is actually resting ──────────────────────────────────────

class TestActualOrderScoring:
    def test_regression_real_order_behind_cutoff_scores_zero(self, runner):
        """Target says 50¢ but the real order rests at 49¢. Competing depth
        (60 ≥ target 50) at 50¢ sets the cutoff at 50 ⇒ our 49¢ order does
        not qualify. Scoring the target would have claimed a share."""
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 49, MIN), _order("no", 49, MIN)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        s = runner._score_market(book, params, target=_target(yes=50, no=50))
        assert s.mode == "actual"
        assert s.is_resting
        assert s.result.snapshot_valid
        assert s.result.yes_cutoff_price == 50 and s.result.no_cutoff_price == 50
        assert s.share == 0.0
        assert s.raw_our_score == 0.0

    def test_old_hypothetical_path_would_have_claimed_credit(self, runner):
        """Sanity for the regression above: with NOTHING resting the target
        is scored hypothetically and gets credit — which is exactly what the
        old code did even when the real order was elsewhere."""
        params = runner.params_by_ticker[TKR]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        s = runner._score_market(book, params, target=_target(yes=50, no=50, size=20))
        assert s.mode == "hypothetical"
        assert not s.is_resting
        assert s.result.our_yes_normalized == pytest.approx(20 / 80)
        assert s.share == 0.0          # phantom ⇒ never credited

    def test_live_mode_does_not_double_count(self, runner):
        """Live: the venue book already contains our 30 at 50¢ (60 total)."""
        params = runner.params_by_ticker[TKR]
        runner.qm.paper = False
        runner.qm.resting[TKR] = [_order("yes", 50, 30), _order("no", 50, 30)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        s = runner._score_market(book, params)
        assert s.is_resting
        assert s.result.our_yes_normalized == pytest.approx(30 / 60)
        assert s.result.yes_total_qualifying_score == pytest.approx(60)
        assert s.share == pytest.approx(30 / 60)

    def test_paper_mode_augments_shadow_orders(self, runner):
        """Paper: shadow orders never reach the venue ⇒ fold them in."""
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 50, 30), _order("no", 50, 30)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        s = runner._score_market(book, params)
        assert s.is_resting
        assert s.result.our_yes_normalized == pytest.approx(30 / 90)
        assert s.result.yes_total_qualifying_score == pytest.approx(90)
        assert s.share == pytest.approx(30 / 90)

    def test_augment_does_not_mutate_source_book(self, runner):
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 50, 20), _order("no", 50, 20)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        runner._score_market(book, params)
        assert book.yes_bids[0].size == 60 and book.no_bids[0].size == 60

    def test_multiple_orders_per_side_all_scored(self, runner):
        params = runner.params_by_ticker[TKR]
        runner.qm.paper = False
        runner.qm.resting[TKR] = [_order("yes", 50, 10, "a"), _order("yes", 49, 10, "b"),
                                  _order("no", 50, MIN)]
        book = _book(yes=[(50, 30), (49, 30)], no=[(50, 60)])   # cutoff yes=49
        s = runner._score_market(book, params)
        expect = (10 + 0.5 * 10) / (30 + 0.5 * 30)
        assert s.result.our_yes_normalized == pytest.approx(expect)

    def test_one_sided_resting_is_not_resting(self, runner):
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 50, MIN)]
        s = runner._score_market(_book(yes=[(50, 60)], no=[(50, 60)]), params)
        assert s.mode == "actual" and not s.is_resting and s.share == 0.0

    def test_sub_min_size_is_not_resting(self, runner):
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 50, max(1, MIN - 1)), _order("no", 50, MIN)]
        s = runner._score_market(_book(yes=[(50, 60)], no=[(50, 60)]), params)
        assert not s.is_resting and s.share == 0.0

    def test_pending_cancel_orders_ignored(self, runner):
        params = runner.params_by_ticker[TKR]
        dead = _order("yes", 50, MIN); dead.pending_cancel = True
        runner.qm.resting[TKR] = [dead, _order("no", 50, MIN)]
        s = runner._score_market(_book(yes=[(50, 60)], no=[(50, 60)]), params)
        assert not s.is_resting

    def test_hypothetical_respects_per_side_overrides(self, runner):
        params = runner.params_by_ticker[TKR]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        t = _target(size=20, yes_size_override=30, no_size_override=10)
        s = runner._score_market(book, params, target=t)
        assert s.result.our_yes_normalized == pytest.approx(30 / 90)
        assert s.result.our_no_normalized == pytest.approx(10 / 70)

    def test_invalid_snapshot_pays_nothing_even_when_resting(self, runner):
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        s = runner._score_market(_book(yes=[(50, 60)], no=[(50, 5)]), params)  # NO side < target
        assert s.is_resting and not s.result.snapshot_valid and s.share == 0.0

    def test_fractional_book_sizes_flow_through(self, runner):
        params = runner.params_by_ticker[TKR]
        runner.qm.paper = False
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        s = runner._score_market(_book(yes=[(50, 49.5), (49, 0.75)], no=[(50, 60.25)]), params)
        assert s.result.snapshot_valid
        assert s.result.yes_cutoff_price == 49


# ── Reward units ──────────────────────────────────────────────────────────

class TestRewardUnits:
    def test_program_params_prefers_pool_and_period(self):
        p = _program_params_from_market(_market())
        assert p.period_reward_usd == 700.0
        assert p.period_seconds == 7 * 86400
        assert p.pool_rate_usd_per_sec == pytest.approx(100.0 / 86400)

    def test_program_params_legacy_rate_fallback(self):
        m = _market(); m.pop("period_reward_usd"); m.pop("period_seconds")
        p = _program_params_from_market(m)
        assert p.period_reward_usd == 100.0 and p.period_seconds == 86400.0
        assert p.pool_rate_usd_per_sec == pytest.approx(100.0 / 86400)

    def test_program_params_null_period_seconds_falls_back(self):
        p = _program_params_from_market(_market(period_seconds=None))
        assert p.period_seconds == 86400.0 and p.period_reward_usd == 100.0

    def test_interval_payout_formula_and_bounds(self):
        p = ProgramParams(TKR, 50, 0.5, period_reward_usd=700.0, period_seconds=7 * 86400)
        assert interval_payout_usd(0.5, p, 10.0) == pytest.approx(0.5 * 700 / (7 * 86400) * 10)
        assert interval_payout_usd(0.0, p, 10.0) == 0.0
        assert interval_payout_usd(0.5, p, 0.0) == 0.0
        assert interval_payout_usd(1.0, p, 10 ** 9) == 700.0          # bounded by pool
        assert interval_payout_usd(0.5, ProgramParams(TKR, 50, 0.5, 0.0, 100), 5) == 0.0

    def test_snapshot_share_is_half_of_total_score(self):
        from engine.lip_scorer import SnapshotScore
        r = SnapshotScore(TKR, True, True, True, 0.4, 0.2, 0.6)
        assert snapshot_share(r) == pytest.approx(0.3)
        r.snapshot_valid = False
        assert snapshot_share(r) == 0.0

    def test_persist_accrues_share_times_rate_times_elapsed(self, runner, db):
        params = runner.params_by_ticker[TKR]
        runner.qm.paper = False
        runner.qm.resting[TKR] = [_order("yes", 50, 30), _order("no", 50, 30)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])       # share = 0.5 each side ⇒ 0.5
        s = runner._score_market(book, params)
        assert s.share == pytest.approx(0.5)
        t0 = 1_800_000_000.0
        assert runner._persist_snapshot(TKR, s, params, t0)
        assert runner._persist_snapshot(TKR, s, params, t0 + 5.0)
        assert runner._persist_snapshot(TKR, s, params, t0 + 5.0 + 3600)   # long gap → unknown state
        rows = _rows(db)
        assert len(rows) == 3
        rate = 700.0 / (7 * 86400)
        assert rows[0][5] == 0.0                                        # first row claims nothing
        assert rows[1][5] == pytest.approx(0.5 * rate * 5.0)            # prior share × interval
        assert rows[2][5] == 0.0                                        # gap > max ⇒ nothing claimed
        assert all(r[6] == 1 for r in rows)                             # was_resting
        assert all(r[7] == pytest.approx(0.5) for r in rows)            # our_share
        assert rows[1][0] == pytest.approx(30 + 30)                     # raw score units

    def test_persist_zero_when_not_resting(self, runner, db):
        params = runner.params_by_ticker[TKR]
        s = runner._score_market(_book(yes=[(50, 60)], no=[(50, 60)]), params, target=_target())
        t0 = 1_800_000_000.0
        runner._persist_snapshot(TKR, s, params, t0)
        runner._persist_snapshot(TKR, s, params, t0 + 5)
        for r in _rows(db):
            assert r[5] == 0.0 and r[6] == 0 and r[7] == 0.0 and r[4] == 0

    def test_persist_throttled_to_5s(self, runner, db):
        params = runner.params_by_ticker[TKR]
        s = runner._score_market(_book(yes=[(50, 60)], no=[(50, 60)]), params)
        t0 = 1_800_000_000.0
        assert runner._persist_snapshot(TKR, s, params, t0)
        assert not runner._persist_snapshot(TKR, s, params, t0 + 1)
        assert runner._persist_snapshot(TKR, s, params, t0 + 5)

    def test_schema_columns_added_to_legacy_table(self, runner, db):
        cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(lip_snapshots)")}
        assert {"was_resting", "our_share"} <= cols


# ── Skips must not leave exposure ─────────────────────────────────────────

class TestSkipHandling:
    def test_transient_reasons_keep_resting(self, runner):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        for reason in TRANSIENT_SKIP_REASONS:
            assert not runner._handle_skip(TKR, reason)
        runner.qm.cancel_all.assert_not_called()

    @pytest.mark.parametrize("reason", ["fair_value", "pre_settlement", "stale_book",
                                        "no_params", "risk_veto:daily_loss", "unknown"])
    def test_non_transient_reasons_cancel(self, runner, reason):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        assert runner._handle_skip(TKR, reason)
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)

    def test_cancel_throttled(self, runner):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        assert runner._handle_skip(TKR, "fair_value")
        assert not runner._handle_skip(TKR, "fair_value")
        assert runner.qm.cancel_all.call_count == 1
        runner._skip_cancel_ts[TKR] -= PaperRunner.SKIP_CANCEL_THROTTLE_SEC + 1
        assert runner._handle_skip(TKR, "fair_value")

    def test_nothing_resting_nothing_to_cancel(self, runner):
        assert not runner._handle_skip(TKR, "fair_value")
        runner.qm.cancel_all.assert_not_called()
        assert runner.skip_counts["fair_value"] == 1

    def test_quote_target_records_reasons(self, runner):
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        book.stale = True
        assert runner._quote_target_for(book) is None
        assert runner._skip_reason[TKR] == "stale_book"
        book.stale = False
        book.no_bids = []
        assert runner._quote_target_for(book) is None
        assert runner._skip_reason[TKR] == "no_best"
        other = _book(yes=[(50, 60)], no=[(50, 60)]); other.market_ticker = "NOT-OURS"
        assert runner._quote_target_for(other) is None
        assert runner._skip_reason["NOT-OURS"] == "no_params"

    def test_on_book_update_skip_cancels_and_does_not_reconcile(self, runner, db):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        runner._quote_target_for = MagicMock(side_effect=lambda b: runner._skip(TKR, "fair_value"))
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        runner.last_score_ts[TKR] = 0
        asyncio.run(runner.on_book_update(book))
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)
        runner.qm.reconcile.assert_not_called()
        assert len(_rows(db)) == 1          # still recorded honestly

    def test_on_book_update_stale_book_no_snapshot(self, runner, db):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        book = _book(yes=[(50, 60)], no=[(50, 60)]); book.stale = True
        runner.last_score_ts[TKR] = 0
        asyncio.run(runner.on_book_update(book))
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)
        assert _rows(db) == []

    def test_on_book_update_volatility_keeps_orders_and_scores_them(self, runner, db):
        runner.qm.paper = False
        runner.qm.resting[TKR] = [_order("yes", 50, 30), _order("no", 50, 30)]
        runner._quote_target_for = MagicMock(side_effect=lambda b: runner._skip(TKR, "volatility"))
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        runner.last_score_ts[TKR] = 0
        asyncio.run(runner.on_book_update(book))
        runner.qm.cancel_all.assert_not_called()
        runner.qm.reconcile.assert_not_called()
        row = _rows(db)[0]
        assert row[6] == 1 and row[7] == pytest.approx(0.5)

    def test_on_book_update_happy_path_reconciles(self, runner, db):
        runner._quote_target_for = MagicMock(return_value=_target(size=20))
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        runner.last_score_ts[TKR] = 0
        asyncio.run(runner.on_book_update(book))
        runner.qm.reconcile.assert_called_once()
        assert runner.reconciles[TKR] == 1
        assert _rows(db)[0][6] == 0        # nothing resting yet ⇒ phantom row


# ── Heartbeat shares the path ─────────────────────────────────────────────

class TestHeartbeatSharedPath:
    def test_heartbeat_scores_actual_orders_via_score_market(self, runner, db, monkeypatch):
        runner.qm.paper = False
        runner.qm.resting[TKR] = [_order("yes", 50, 30), _order("no", 50, 30)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        ws = MagicMock(); ws.books = {TKR: book}
        runner.qm.periodic_resync = MagicMock(return_value={})
        runner.sizer.update_target_shares_from_db = MagicMock(return_value=0)
        runner._pre_settlement_cancel = MagicMock(return_value=0)
        runner._cancel_zombie_quotes = MagicMock(return_value=0)
        monkeypatch.setattr(settings, "USE_MICROPRICE", False)
        calls = []
        real = runner._score_market

        def spy(b, p, target=None):
            calls.append((b.market_ticker, target))
            return real(b, p, target)
        runner._score_market = spy

        async def one_beat():
            task = asyncio.ensure_future(runner.heartbeat_snapshot_loop(ws, interval_sec=0))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if _rows(db):
                    break
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        asyncio.run(one_beat())
        assert calls and calls[0] == (TKR, None)      # no hypothetical target in heartbeat
        row = _rows(db)[0]
        assert row[6] == 1 and row[7] == pytest.approx(0.5)
        assert runner.snapshots_valid[TKR] >= 1
