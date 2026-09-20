"""Regressions for the 2026-09-20 review of 90a15d1 — consistent state
across fills, books, programs and accounting.

Reviewer's reproductions, pinned here:
  1. Replaying one trade_id twice took remaining 100 → 80 (correct: 90).
  2. A program that ended 15 min ago returned NO blocking reason; expiry
     waited for the next discovery cycle.
  3. _on_fill changed order quantity but neither recomputed nor broke the
     stored accrual share, so a fully-filled order kept its old earning
     rate until the next scoring event.
  4. _periodic_discover ran blocking scan/network work on the event loop,
     stalling feed processing and cancellation.
Plus discovery freshness: failed/partial scans must not leave stale rows
eligible, and existing tickers must get refreshed parameters.

Fill field names follow Kalshi's documented user-fills payload
(trade_id, order_id, market_ticker, count_fp, yes_price_dollars), with
exchange timestamp and subaccount preserved.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine import lip_discovery
from engine.lip_discovery import DiscoveryResult, discover_result, last_complete_scan_ts
from engine.lip_scorer import ProgramParams
from execution.kalshi_ws import BookLevel, BookState, FillEvent, KalshiWS
from execution.quote_manager import QuoteManager, RestingOrder
from run_paper import PaperRunner, _program_params_from_market

TKR = "KXTEST-26SEP30-T1"
MIN = settings.MIN_QUOTE_SIZE_CONTRACTS
T0 = 1_800_000_000.0


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE lip_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_ticker TEXT, captured_at TEXT,
            our_score REAL, total_score REAL, yes_qualified INTEGER, no_qualified INTEGER,
            snapshot_valid INTEGER, estimated_payout_usd REAL, was_resting INTEGER, our_share REAL
        );
        CREATE TABLE quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_ticker TEXT, side TEXT,
            price_cents INTEGER, size_contracts INTEGER, order_id TEXT, status TEXT,
            paper INTEGER, placed_at TEXT, cancelled_at TEXT, filled_at TEXT,
            fill_size INTEGER, fill_price_cents INTEGER, notes TEXT
        );
        CREATE TABLE lip_programs (
            id TEXT PRIMARY KEY, market_ticker TEXT, series_ticker TEXT,
            start_date TEXT, end_date TEXT, period_reward_usd REAL, period_seconds REAL,
            discount_factor REAL, target_size REAL, paid_out INTEGER,
            enrolled INTEGER, blocked_reason TEXT, reward_per_day_usd REAL, last_seen TEXT
        );
        CREATE TABLE settlement_log (
            id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, series_prefix TEXT,
            close_time TEXT, our_realized_usd REAL, recorded_at TEXT
        );
        CREATE TABLE fill_ledger (
            trade_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT, side TEXT,
            count INTEGER, count_real REAL, yes_price_cents INTEGER, no_price_cents INTEGER,
            is_taker INTEGER, created_at TEXT, synced_at TEXT
        );
    """)
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
    r.qm.cancel_all = MagicMock(return_value=1)
    r._refresh_blacklist = MagicMock()
    r._is_blacklisted = MagicMock(return_value=False)
    r.qm.reconcile = MagicMock(return_value={"action": "ok"})
    r.last_complete_scan_ts = time.time()
    return r


@pytest.fixture
def live_qm(db):
    qm = QuoteManager(paper=True, db_path=db)
    qm.paper = False
    return qm


def _order(side, price, size, oid=None, coid="LIP-x", paper=False):
    return RestingOrder(order_id=oid or f"{side}-{price}", market_ticker=TKR, side=side,
                        price_cents=price, size_contracts=float(size), placed_at=time.time(),
                        paper=paper, client_order_id=coid)


def _book(yes, no, ticker=TKR):
    b = BookState(market_ticker=ticker)
    b.yes_bids = sorted([BookLevel(p, float(s)) for p, s in yes], key=lambda l: -l.price_cents)
    b.no_bids = sorted([BookLevel(p, float(s)) for p, s in no], key=lambda l: -l.price_cents)
    b.snapshot_count = 1
    return b


def _venue(order_id, side="yes", price="0.5000", remaining="100.00", coid="LIP-abc",
           status="resting", ticker=TKR):
    return {"order_id": order_id, "ticker": ticker, "side": side, "status": status,
            f"{side}_price_dollars": price, "remaining_count_fp": remaining,
            "initial_count_fp": "100.00", "client_order_id": coid,
            "created_time": "2026-09-20T12:00:00Z"}


class _FakeClient:
    def __init__(self, orders):
        self.orders = orders

    def get(self, path, params=None):
        return {"orders": list(self.orders), "cursor": None}


def _fill(count="10.00", trade_id="t1", order_id="o1", side="yes", ticker=TKR):
    """Kalshi user-fills shape (documented fields)."""
    return {"trade_id": trade_id, "order_id": order_id, "market_ticker": ticker,
            "side": side, "count_fp": count, "yes_price_dollars": "0.5000",
            "no_price_dollars": "0.5000", "is_taker": False,
            "ts": 1758369600, "subaccount_id": "sub-7"}


# ── 1. Duplicate fills ────────────────────────────────────────────────────

class TestFillIdempotency:
    def test_reviewer_reproduction_same_trade_id_twice(self, live_qm):
        """100 − 10 applied once = 90. Replay must NOT take it to 80."""
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        ev = KalshiWS._parse_fill(_fill(count="10.00", trade_id="t1"))
        live_qm.apply_fill(ev.order_id, ev.market_ticker, ev.count, trade_id=ev.trade_id,
                           side=ev.side, price_cents=50)
        assert live_qm.last_fill_status == "applied"
        live_qm.apply_fill(ev.order_id, ev.market_ticker, ev.count, trade_id=ev.trade_id,
                           side=ev.side, price_cents=50)
        assert live_qm.last_fill_status == "duplicate"
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(90.0)
        assert live_qm.fill_stats == {"applied": 1, "duplicate": 1, "untracked": 0,
                                      "unknown_order": 0}

    def test_distinct_trade_ids_both_apply(self, live_qm):
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        for tid in ("t1", "t2"):
            live_qm.apply_fill("o1", TKR, 10.0, trade_id=tid, side="yes", price_cents=50)
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(80.0)

    def test_duplicate_rejected_across_restart_via_ledger(self, live_qm, db):
        """A trade_id already in fill_ledger is a duplicate even with an
        empty in-memory cache (process restart, or fills_sync got there first)."""
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        live_qm.apply_fill("o1", TKR, 10.0, trade_id="t1", side="yes", price_cents=50)
        fresh = QuoteManager(paper=True, db_path=db)
        fresh.paper = False
        fresh.resting[TKR] = [_order("yes", 50, 90, oid="o1")]
        fresh.apply_fill("o1", TKR, 10.0, trade_id="t1", side="yes", price_cents=50)
        assert fresh.last_fill_status == "duplicate"
        assert fresh.resting[TKR][0].size_contracts == pytest.approx(90.0)

    def test_fill_written_to_ledger_with_exchange_ts_and_subaccount(self, live_qm, db):
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        ev = KalshiWS._parse_fill(_fill())
        live_qm.apply_fill(ev.order_id, ev.market_ticker, ev.count, trade_id=ev.trade_id,
                           side=ev.side, price_cents=50, is_taker=ev.is_taker,
                           exchange_ts=ev.exchange_ts, subaccount=ev.subaccount)
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT order_id, ticker, side, count_real, yes_price_cents, "
                           "exchange_ts, subaccount FROM fill_ledger WHERE trade_id='t1'").fetchone()
        conn.close()
        assert row == ("o1", TKR, "yes", 10.0, 50, 1758369600.0, "sub-7")

    def test_parse_fill_preserves_documented_fields(self):
        ev = KalshiWS._parse_fill(_fill())
        assert (ev.trade_id, ev.order_id, ev.market_ticker) == ("t1", "o1", TKR)
        assert ev.count == 10.0 and ev.price_cents_exact == 50.0
        assert ev.exchange_ts == 1758369600.0 and ev.subaccount == "sub-7"

    def test_fill_without_trade_id_is_applied_but_counted(self, live_qm):
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        live_qm.apply_fill("o1", TKR, 10.0, side="yes", price_cents=50)
        assert live_qm.fill_stats["untracked"] == 1
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(90.0)

    def test_unknown_order_reported(self, live_qm):
        assert live_qm.apply_fill("nope", TKR, 1.0, trade_id="t9", side="yes") is None
        assert live_qm.last_fill_status == "unknown_order"

    def test_concurrent_duplicate_replays_are_serialized(self, live_qm):
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            live_qm.apply_fill("o1", TKR, 10.0, trade_id="t1", side="yes", price_cents=50)
        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(90.0)
        assert live_qm.fill_stats["applied"] == 1


# ── 2. REST / fill ordering ───────────────────────────────────────────────

class TestRestFillOrdering:
    def test_stale_snapshot_cannot_undo_a_newer_fill(self, live_qm):
        """Snapshot taken BEFORE the fill still says 100; local is 90."""
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        live_qm.apply_fill("o1", TKR, 10.0, trade_id="t1", side="yes", price_cents=50)
        live_qm.client = _FakeClient([_venue("o1", remaining="100.00")])   # pre-fill view
        res = live_qm.periodic_resync()
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(90.0)
        assert res["kept_local"] == 1 and res["updated"] == 0

    def test_newer_snapshot_still_corrects_downward(self, live_qm):
        """Fills we never saw on the socket must still reduce us."""
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        live_qm.client = _FakeClient([_venue("o1", remaining="25.50")])
        live_qm.periodic_resync()
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(25.5)

    def test_fill_then_snapshot_no_double_subtraction(self, live_qm):
        """The same execution seen on BOTH paths subtracts once."""
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        live_qm.apply_fill("o1", TKR, 10.0, trade_id="t1", side="yes", price_cents=50)
        live_qm.client = _FakeClient([_venue("o1", remaining="90.00")])    # venue agrees
        live_qm.periodic_resync()
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(90.0)

    def test_full_fill_between_snapshots_is_not_readopted(self, live_qm):
        """Order fully filled after the snapshot was taken: the stale
        snapshot lists it as resting, but it must stay gone."""
        live_qm.resting[TKR] = [_order("yes", 50, 10, oid="o1")]
        live_qm.apply_fill("o1", TKR, 10.0, trade_id="t1", side="yes", price_cents=50)
        assert TKR not in live_qm.resting
        live_qm.client = _FakeClient([_venue("o1", remaining="10.00")])    # pre-fill view
        live_qm.periodic_resync()
        assert TKR not in live_qm.resting

    def test_cancelled_order_is_not_readopted_by_stale_snapshot(self, live_qm):
        o = _order("yes", 50, 10, oid="o1")
        live_qm.resting[TKR] = [o]
        live_qm.client = _FakeClient([])
        live_qm.client.delete = MagicMock()
        live_qm._cancel_order(o)
        live_qm.client = _FakeClient([_venue("o1", remaining="10.00")])
        live_qm.periodic_resync()
        assert TKR not in live_qm.resting

    def test_tombstone_expires_so_genuine_reorder_is_adopted(self, live_qm):
        live_qm.resting[TKR] = [_order("yes", 50, 10, oid="o1")]
        live_qm.apply_fill("o1", TKR, 10.0, trade_id="t1", side="yes", price_cents=50)
        live_qm._tombstones["o1"] -= QuoteManager.TOMBSTONE_TTL_SEC + 1
        live_qm.client = _FakeClient([_venue("o1", remaining="10.00")])
        live_qm.periodic_resync()
        assert live_qm.resting[TKR][0].order_id == "o1"

    def test_resync_and_fills_do_not_interleave(self, live_qm):
        """A resync in flight while fills arrive leaves a consistent total."""
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1")]
        live_qm.client = _FakeClient([_venue("o1", remaining="100.00")])
        done = threading.Event()

        def resyncs():
            while not done.is_set():
                live_qm.periodic_resync()
        t = threading.Thread(target=resyncs); t.start()
        try:
            for i in range(20):
                live_qm.apply_fill("o1", TKR, 1.0, trade_id=f"t{i}", side="yes", price_cents=50)
        finally:
            done.set(); t.join()
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(80.0)


# ── 3. Fills update accrual + inventory ───────────────────────────────────

def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT estimated_payout_usd, our_share, was_resting "
                            "FROM lip_snapshots ORDER BY id").fetchall()
    finally:
        conn.close()


def _scored(share):
    s = MagicMock()
    s.share = share
    s.is_resting = share > 0
    s.raw_our_score = 0.0
    r = MagicMock()
    r.snapshot_valid = share > 0
    r.yes_qualified = r.no_qualified = share > 0
    r.yes_total_qualifying_score = r.no_total_qualifying_score = 60.0
    s.result = r
    return s


def _ev(**kw):
    return KalshiWS._parse_fill(_fill(**kw))


class TestFillUpdatesAccrual:
    def test_partial_fill_breaks_chain_so_old_rate_is_not_credited(self, runner, db):
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=True),
                                  _order("no", 50, 100, oid="o2", paper=True)]
        runner._persist_snapshot(TKR, _scored(0.9), params, T0)
        assert runner._accrual[TKR].last_share == pytest.approx(0.9)
        runner.on_fill(_ev(count="50.00", trade_id="t1", order_id="o1"))
        assert runner._accrual[TKR].last_ts is None          # chain broken
        runner._persist_snapshot(TKR, _scored(0.4), params, T0 + 5)
        assert _rows(db)[1][0] == 0.0                        # nothing at the old 0.9 rate

    def test_full_fill_between_snapshots_stops_earning_immediately(self, runner, db):
        """Reviewer's case: a fully filled order kept its previous rate."""
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 50, 10, oid="o1", paper=True),
                                  _order("no", 50, 10, oid="o2", paper=True)]
        runner._persist_snapshot(TKR, _scored(1.0), params, T0)
        runner.on_fill(_ev(count="10.00", trade_id="t1", order_id="o1"))
        st = runner._accrual[TKR]
        assert st.last_share == 0.0                          # known-flat, not 1.0
        assert not runner._actually_resting(TKR)
        st.last_ts = T0 + 1
        runner._persist_snapshot(TKR, _scored(0.0), params, T0 + 5)
        assert _rows(db)[1][0] == 0.0

    def test_duplicate_fill_does_not_touch_accrual(self, runner):
        params = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=True),
                                  _order("no", 50, 100, oid="o2", paper=True)]
        runner._persist_snapshot(TKR, _scored(0.9), params, T0)
        runner.on_fill(_ev(count="10.00", trade_id="t1", order_id="o1"))
        runner._accrual[TKR].last_ts = T0 + 1
        runner._accrual[TKR].last_share = 0.5
        assert runner.on_fill(_ev(count="10.00", trade_id="t1", order_id="o1")) == "duplicate"
        st = runner._accrual[TKR]
        assert st.last_ts == T0 + 1 and st.last_share == 0.5   # untouched
        assert runner.qm.resting[TKR][0].size_contracts == pytest.approx(90.0)

    def test_fill_invalidates_inventory_cache(self, runner):
        runner.qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=True),
                                  _order("no", 50, 100, oid="o2", paper=True)]
        runner.qm._refresh_inventory(TKR)
        assert TKR in runner.qm.inventory
        runner.on_fill(_ev(count="10.00", trade_id="t1", order_id="o1"))
        assert TKR not in runner.qm.inventory           # forced re-read

    def test_fill_counts_surface(self, runner):
        runner.qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=True),
                                  _order("no", 50, 100, oid="o2", paper=True)]
        runner.on_fill(_ev(count="10.00", trade_id="t1", order_id="o1"))
        runner.on_fill(_ev(count="10.00", trade_id="t1", order_id="o1"))
        assert runner.fill_counts["applied"] == 1 and runner.fill_counts["duplicate"] == 1


# ── 4. Program expiry + discovery freshness at the gate ───────────────────

class TestExpiryGate:
    def test_reviewer_reproduction_expired_15_minutes_ago(self, runner):
        """A program that ended 15 min ago must block, without waiting for
        the next discovery cycle."""
        params = runner.params_by_ticker[TKR]
        params.end_ts = time.time() - 900
        runner.qm.resting[TKR] = [_order("yes", 50, MIN, paper=True),
                                  _order("no", 50, MIN, paper=True)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        assert runner._exposure_gate(book) == "program_expired"
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)

    def test_expiry_blocks_even_inside_scoring_throttle(self, runner):
        params = runner.params_by_ticker[TKR]
        params.end_ts = time.time() - 900
        runner.qm.resting[TKR] = [_order("yes", 50, MIN, paper=True),
                                  _order("no", 50, MIN, paper=True)]
        runner.last_score_ts[TKR] = time.time()
        asyncio.run(runner.on_book_update(_book(yes=[(50, 60)], no=[(50, 60)])))
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)

    def test_not_started_blocks(self, runner):
        params = runner.params_by_ticker[TKR]
        params.start_ts = time.time() + 900
        assert runner._exposure_gate(_book(yes=[(50, 60)], no=[(50, 60)])) == "program_not_started"

    def test_expiry_breaks_accrual(self, runner):
        params = runner.params_by_ticker[TKR]
        st = runner._accrual_for(TKR, params); st.last_ts = T0; st.last_share = 0.8
        params.end_ts = time.time() - 900
        runner._exposure_gate(_book(yes=[(50, 60)], no=[(50, 60)]))
        assert runner._accrual[TKR].last_ts is None

    def test_heartbeat_cancels_expired_independently(self, runner, db, monkeypatch):
        params = runner.params_by_ticker[TKR]
        params.end_ts = time.time() - 900
        runner.qm.resting[TKR] = [_order("yes", 50, MIN, paper=True),
                                  _order("no", 50, MIN, paper=True)]
        runner.qm.periodic_resync = MagicMock(return_value={})
        runner.sizer.update_target_shares_from_db = MagicMock(return_value=0)
        runner._pre_settlement_cancel = MagicMock(return_value=0)
        runner._cancel_zombie_quotes = MagicMock(return_value=0)
        monkeypatch.setattr(settings, "USE_MICROPRICE", False)
        ws = MagicMock(); ws.books = {TKR: _book(yes=[(50, 60)], no=[(50, 60)])}; ws.connected = True

        async def go():
            task = asyncio.ensure_future(runner.heartbeat_snapshot_loop(ws, interval_sec=0))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if runner.qm.cancel_all.called:
                    break
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        asyncio.run(go())
        runner.qm.cancel_all.assert_called_with(market_ticker=TKR, only_ours=True)
        assert _rows(db) == []


class TestDiscoveryFreshnessGate:
    def test_never_completed_blocks(self, runner):
        runner.last_complete_scan_ts = None
        assert runner._exposure_gate(_book(yes=[(50, 60)], no=[(50, 60)])) == "discovery_never_completed"

    def test_stale_scan_blocks_and_cancels(self, runner):
        runner.last_complete_scan_ts = time.time() - settings.DISCOVERY_MAX_AGE_SEC - 1
        runner.qm.resting[TKR] = [_order("yes", 50, MIN, paper=True),
                                  _order("no", 50, MIN, paper=True)]
        assert runner._exposure_gate(_book(yes=[(50, 60)], no=[(50, 60)])) == "discovery_stale"
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)

    def test_fresh_scan_passes(self, runner):
        runner.last_complete_scan_ts = time.time()
        assert runner._exposure_gate(_book(yes=[(50, 60)], no=[(50, 60)])) is None

    def test_note_discovery_only_advances_on_complete(self, runner):
        runner.last_complete_scan_ts = None
        runner.note_discovery(DiscoveryResult([], False, T0, T0 + 5, errors=["boom"]))
        assert runner.last_complete_scan_ts is None
        runner.note_discovery(DiscoveryResult([], True, T0, T0 + 5))
        assert runner.last_complete_scan_ts == T0 + 5

    def test_freshness_seeded_from_db_on_construction(self, db):
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE discovery_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
            complete INTEGER, n_programs INTEGER, n_rejected INTEGER, n_demoted INTEGER, errors TEXT)""")
        finished = "2026-09-20T12:00:00+00:00"
        conn.execute("INSERT INTO discovery_runs (started_at, finished_at, complete) "
                     "VALUES ('2026-09-20T11:00:00+00:00', ?, 1)", (finished,))
        conn.commit(); conn.close()
        expected = datetime.fromisoformat(finished).timestamp()
        r = PaperRunner([_market()])
        assert r.last_complete_scan_ts == pytest.approx(expected)


class _FakeIncentiveClient:
    """Serves /incentive_programs; `fail_on` statuses raise."""
    def __init__(self, rows, fail_on=()):
        self.rows = rows
        self.fail_on = set(fail_on)
        self.calls = []

    def get_unauth(self, path, params=None):
        s = (params or {}).get("status")
        self.calls.append(s)
        if s in self.fail_on:
            raise RuntimeError("upstream 503")
        return {"incentive_programs": [r for r in self.rows if r.get("_status") == s],
                "next_cursor": None}


def _raw(ticker=TKR, status="active", reward=1_000_000, target="100", df=5000,
         start="2026-01-01T00:00:00Z", end="2028-01-01T00:00:00Z"):
    return {"_status": status, "id": f"p-{ticker}", "market_ticker": ticker,
            "incentive_type": "liquidity", "period_reward": reward,
            "discount_factor_bps": df, "target_size_fp": target,
            "start_date": start, "end_date": end, "paid_out": False}


class TestScanCompleteness:
    def test_complete_scan_records_freshness(self, db):
        client = _FakeIncentiveClient([_raw()])
        with patch.object(lip_discovery, "KalshiClient", lambda: client):
            res = discover_result(save=True)
        assert res.complete and not res.errors
        assert last_complete_scan_ts(db) == pytest.approx(res.finished_ts, abs=2)

    def test_partial_scan_is_not_complete_and_does_not_advance_freshness(self, db):
        client = _FakeIncentiveClient([_raw()], fail_on={"closed"})
        with patch.object(lip_discovery, "KalshiClient", lambda: client):
            res = discover_result(save=True)
        assert not res.complete and res.errors
        assert last_complete_scan_ts(db) is None

    def test_complete_scan_demotes_rows_it_did_not_see(self, db):
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO lip_programs (id, market_ticker, series_ticker, start_date, "
                     "end_date, period_reward_usd, discount_factor, target_size, paid_out, "
                     "enrolled, reward_per_day_usd, last_seen) VALUES "
                     "('old', 'KXOLD-1', 'KXOLD', '2026-01-01', '2028-01-01', 700, 0.5, 50, 0, 1, "
                     "100, '2026-09-19T00:00:00+00:00')")
        conn.commit(); conn.close()
        client = _FakeIncentiveClient([_raw()])
        with patch.object(lip_discovery, "KalshiClient", lambda: client):
            res = discover_result(save=True)
        assert res.complete and res.n_demoted == 1
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT enrolled, blocked_reason FROM lip_programs "
                           "WHERE market_ticker='KXOLD-1'").fetchone()
        conn.close()
        assert row == (0, "not_in_scan")

    def test_partial_scan_demotes_nothing(self, db):
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO lip_programs (id, market_ticker, series_ticker, start_date, "
                     "end_date, period_reward_usd, discount_factor, target_size, paid_out, "
                     "enrolled, reward_per_day_usd, last_seen) VALUES "
                     "('old', 'KXOLD-1', 'KXOLD', '2026-01-01', '2028-01-01', 700, 0.5, 50, 0, 1, "
                     "100, '2026-09-19T00:00:00+00:00')")
        conn.commit(); conn.close()
        client = _FakeIncentiveClient([_raw()], fail_on={"upcoming"})
        with patch.object(lip_discovery, "KalshiClient", lambda: client):
            res = discover_result(save=True)
        assert not res.complete and res.n_demoted == 0
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT enrolled FROM lip_programs "
                            "WHERE market_ticker='KXOLD-1'").fetchone()[0] == 1
        conn.close()

    def test_single_status_scan_is_never_complete(self, db):
        client = _FakeIncentiveClient([_raw()])
        with patch.object(lip_discovery, "KalshiClient", lambda: client):
            res = discover_result(status="active", save=True)
        assert not res.complete          # a slice is not the universe

    def test_run_recorded_for_both_outcomes(self, db):
        client = _FakeIncentiveClient([_raw()], fail_on={"closed"})
        with patch.object(lip_discovery, "KalshiClient", lambda: client):
            discover_result(save=True)
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT complete, errors FROM discovery_runs ORDER BY id DESC "
                           "LIMIT 1").fetchone()
        conn.close()
        assert row[0] == 0 and "closed" in row[1]


# ── 5. Parameters refreshed for existing tickers ──────────────────────────

class TestParamRefresh:
    def test_existing_ticker_gets_new_parameters(self, runner):
        upd = runner.refresh_params([_market(target_size=200, period_reward_usd=1400.0,
                                             discount_factor=0.9)])
        assert upd["changed"] == 1 and upd["reprogrammed"] == 0
        p = runner.params_by_ticker[TKR]
        assert (p.target_size, p.period_reward_usd, p.discount_factor) == (200.0, 1400.0, 0.9)
        assert runner.markets[0]["target_size"] == 200

    def test_unchanged_program_is_a_noop(self, runner):
        before = runner.params_by_ticker[TKR]
        assert runner.refresh_params([_market()])["changed"] == 0
        assert runner.params_by_ticker[TKR] is before

    def test_changed_window_same_ticker_resets_accrual(self, runner):
        """A re-listed ticker is a NEW pool: the old cumulative cap and
        accrual must not carry over."""
        params = runner.params_by_ticker[TKR]
        st = runner._accrual_for(TKR, params)
        st.accrued_usd = 500.0; st.last_ts = T0; st.last_share = 0.7
        upd = runner.refresh_params([_market(start_date="2028-01-01T00:00:00Z",
                                             end_date="2028-01-08T00:00:00Z")])
        assert upd["reprogrammed"] == 1
        assert TKR not in runner._accrual
        fresh = runner._accrual_for(TKR, runner.params_by_ticker[TKR])
        assert fresh.accrued_usd == 0.0 and fresh.last_ts is None

    def test_unknown_ticker_ignored(self, runner):
        assert runner.refresh_params([_market(market_ticker="KXNEW-1")])["changed"] == 0
        assert "KXNEW-1" not in runner.params_by_ticker

    def test_refreshed_target_size_changes_qualification(self, runner):
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        p = runner.params_by_ticker[TKR]
        assert runner._qualification_adjust(book, p, 50, 50, 25, None, None)[0] is None
        runner.refresh_params([_market(target_size=500)])
        p2 = runner.params_by_ticker[TKR]
        reason, yes_ov, _ = runner._qualification_adjust(book, p2, 50, 50, 25, None, None)
        assert reason is not None or yes_ov == 440     # must react to the new cliff


# ── 6. Discovery off the event loop ───────────────────────────────────────

class TestDiscoveryOffLoop:
    def test_blocking_scan_does_not_stall_the_loop(self):
        """The discovery cycle must run in an executor: a 200ms blocking
        scan may not delay feed processing (and therefore cancellation)."""
        def blocking_cycle():
            time.sleep(0.2)
            return DiscoveryResult([], True, time.time(), time.time()), []

        ticks = []

        async def scenario():
            async def ticker():
                for _ in range(20):
                    await asyncio.sleep(0.01)
                    ticks.append(time.monotonic())

            async def cycle():
                loop_ = asyncio.get_running_loop()
                await loop_.run_in_executor(None, blocking_cycle)
            await asyncio.gather(cycle(), ticker())
        asyncio.run(scenario())
        max_gap = max(ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1))
        assert max_gap < 0.05, f"event loop blocked {max_gap * 1000:.0f}ms during discovery"

    def test_runner_discovery_helpers_are_sync_and_callable_off_loop(self):
        """retire_inactive_markets / refresh_params / note_discovery are plain
        callables so the loop can hand them to an executor."""
        for name in ("retire_inactive_markets", "refresh_params", "note_discovery", "on_fill"):
            fn = getattr(PaperRunner, name)
            assert not asyncio.iscoroutinefunction(fn), f"{name} must not be async"
