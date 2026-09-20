"""Regressions for the 2026-09-20 external review of commit 55a5574.

Reproductions the reviewer verified, pinned here:
  1. Critical — a stale notification arriving within the 1s scoring
     throttle produced ZERO cancellation-handler calls; the heartbeat also
     skipped stale books without cancelling.
  2. High — after a partial fill (venue remaining 25.50) periodic_resync
     kept the local size at 100.
  3. High — $0.4950 and $0.5040 both became 50¢ (distinct levels collapsed).
  4. High — _persist_snapshot credited the LATEST share backward over the
     elapsed interval, even across missing observations, with only a
     per-row pool cap.
Plus the reopened items: qualification-aware placement, retirement of
inactive programs on discovery refresh, and the live-execution interlock.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine.lip_scorer import ProgramParams
from execution import kalshi_ws as kw
from execution.kalshi_ws import BookLevel, BookState, KalshiWS
from execution.quote_manager import QuoteManager, RestingOrder
import run_paper as rp
from run_paper import PaperRunner, _program_params_from_market

TKR = "KXTEST-26SEP30-T1"
MIN = settings.MIN_QUOTE_SIZE_CONTRACTS
T0 = 1_800_000_000.0


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "rev.db"
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
    """)
    conn.commit(); conn.close()
    monkeypatch.setattr(settings, "DB_PATH", str(path))
    return str(path)


def _market(**kw):
    # 7-day window covering T0 (2027-01-15T08:00:00Z): 2027-01-14 → 2027-01-21
    m = dict(market_ticker=TKR, target_size=50, discount_factor=0.5,
             reward_per_day_usd=100.0, period_reward_usd=700.0, period_seconds=7 * 86400.0,
             start_date="2027-01-14T00:00:00Z", end_date="2027-01-21T00:00:00Z")
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
    return r


def _order(side, price, size, oid=None, coid="LIP-x", paper=True):
    return RestingOrder(order_id=oid or f"{side}-{price}", market_ticker=TKR, side=side,
                        price_cents=price, size_contracts=float(size), placed_at=time.time(),
                        paper=paper, client_order_id=coid)


def _book(yes, no, ticker=TKR):
    b = BookState(market_ticker=ticker)
    b.yes_bids = sorted([BookLevel(p, float(s)) for p, s in yes], key=lambda l: -l.price_cents)
    b.no_bids = sorted([BookLevel(p, float(s)) for p, s in no], key=lambda l: -l.price_cents)
    b.snapshot_count = 1
    return b


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT estimated_payout_usd, our_share, was_resting "
                            "FROM lip_snapshots ORDER BY id").fetchall()
    finally:
        conn.close()


class _TestWS(KalshiWS):
    def _load_key(self):
        self._private_key = None


class _FakeSock:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _ws():
    w = _TestWS(api_key="k", private_key_path="/nonexistent")
    w._ws = _FakeSock()
    w.connected = True
    return w


def _snap(ticker=TKR, sid=7, seq=1, yes=None, no=None):
    return json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": seq,
                       "msg": {"market_ticker": ticker, "yes_dollars_fp": yes or [],
                               "no_dollars_fp": no or []}})


def _delta(ticker=TKR, sid=7, seq=2, side="yes", price="0.5000", delta="1"):
    return json.dumps({"type": "orderbook_delta", "sid": sid, "seq": seq,
                       "msg": {"market_ticker": ticker, "side": side,
                               "price_dollars": price, "delta_fp": delta}})


# ── 1. Critical: cancellation ahead of throttles ─────────────────────────

class TestCancelAheadOfThrottle:
    def test_stale_within_throttle_window_still_cancels(self, runner):
        """Reviewer's reproduction: an update was just scored (throttle is
        hot), then the stale notification arrives. It must cancel."""
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        runner.last_score_ts[TKR] = time.time()          # throttle hot
        book.stale = True
        asyncio.run(runner.on_book_update(book))
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)

    def test_stale_bypasses_skip_cancel_throttle(self, runner):
        """A non-transient skip cancelled 1s ago; stale must not wait 30s."""
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        runner._skip_cancel_ts[TKR] = time.time()
        book = _book(yes=[(50, 60)], no=[(50, 60)]); book.stale = True
        asyncio.run(runner.on_book_update(book))
        assert runner.qm.cancel_all.call_count == 1

    def test_ws_stale_transition_reaches_runner_through_throttle(self, runner):
        """End to end: WS seq gap → stale callback → runner cancels, even
        though the runner scored this market a moment ago."""
        ws = _ws()
        ws.books[TKR] = BookState(market_ticker=TKR)
        ws.on_update(runner.on_book_update)
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]

        async def go():
            await ws._handle_message(_snap(seq=1, yes=[["0.5000", "60"]], no=[["0.5000", "60"]]))
            runner.last_score_ts[TKR] = time.time()      # hot throttle
            await ws._handle_message(_delta(seq=3))        # seq 2 lost → stale
        asyncio.run(go())
        assert ws.books[TKR].stale
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)

    def test_unsupported_grid_cancels_ahead_of_throttle(self, runner):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        book = _book(yes=[(50, 60)], no=[(50, 60)]); book.unsupported_grid = True
        runner.last_score_ts[TKR] = time.time()
        asyncio.run(runner.on_book_update(book))
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)

    def test_retired_market_cancels_ahead_of_throttle(self, runner):
        other = "KXGONE-26SEP30-T1"
        runner.qm.resting[other] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        book = _book(yes=[(50, 60)], no=[(50, 60)], ticker=other)
        runner.last_score_ts[other] = time.time()
        asyncio.run(runner.on_book_update(book))
        runner.qm.cancel_all.assert_called_once_with(market_ticker=other, only_ours=True)

    def test_healthy_update_within_throttle_is_still_throttled(self, runner):
        runner._quote_target_for = MagicMock()
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        runner.last_score_ts[TKR] = time.time()
        asyncio.run(runner.on_book_update(book))
        runner._quote_target_for.assert_not_called()
        runner.qm.cancel_all.assert_not_called()


def _one_heartbeat(runner, ws, db, monkeypatch, wait_for=None):
    runner.qm.periodic_resync = MagicMock(return_value={})
    runner.sizer.update_target_shares_from_db = MagicMock(return_value=0)
    runner._pre_settlement_cancel = MagicMock(return_value=0)
    runner._cancel_zombie_quotes = MagicMock(return_value=0)
    monkeypatch.setattr(settings, "USE_MICROPRICE", False)

    async def go():
        task = asyncio.ensure_future(runner.heartbeat_snapshot_loop(ws, interval_sec=0))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if wait_for is None or wait_for():
                break
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    asyncio.run(go())


class TestHeartbeatCancelsIndependently:
    def test_stale_book_cancels_in_heartbeat(self, runner, db, monkeypatch):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        book = _book(yes=[(50, 60)], no=[(50, 60)]); book.stale = True
        ws = MagicMock(); ws.books = {TKR: book}; ws.connected = True
        _one_heartbeat(runner, ws, db, monkeypatch, wait_for=lambda: runner.qm.cancel_all.called)
        runner.qm.cancel_all.assert_called_with(market_ticker=TKR, only_ours=True)
        assert _rows(db) == []                       # skipped for scoring AND cancelled

    def test_disconnected_ws_cancels_in_heartbeat(self, runner, db, monkeypatch):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        ws = MagicMock(); ws.books = {TKR: book}; ws.connected = False
        _one_heartbeat(runner, ws, db, monkeypatch, wait_for=lambda: runner.qm.cancel_all.called)
        runner.qm.cancel_all.assert_called_with(market_ticker=TKR, only_ours=True)

    def test_orphan_resting_orders_swept(self, runner, db, monkeypatch):
        orphan = "KXORPHAN-26SEP30-T1"
        runner.qm.resting[orphan] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        ws = MagicMock(); ws.books = {}; ws.connected = True
        _one_heartbeat(runner, ws, db, monkeypatch, wait_for=lambda: runner.qm.cancel_all.called)
        runner.qm.cancel_all.assert_called_with(market_ticker=orphan, only_ours=True)

    def test_foreign_orders_are_not_swept(self, runner):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN, coid="", paper=False)]
        assert not runner._handle_skip(TKR, "stale_book", force=True)
        runner.qm.cancel_all.assert_not_called()


class TestDisconnect:
    def test_mark_disconnected_stales_books_and_notifies(self):
        ws = _ws()
        for t in ("A", "B"):
            ws.books[t] = BookState(market_ticker=t, snapshot_count=1)
        got = []

        async def cb(tickers):
            got.append(sorted(tickers))
        ws.on_disconnect(cb)
        asyncio.run(ws._mark_disconnected())
        assert not ws.connected
        assert got == [["A", "B"]]
        assert all(b.stale and b.stale_reason == "disconnect" for b in ws.books.values())
        assert not any(b.is_usable() for b in ws.books.values())

    def test_pull_all_exposure_forces_cancel_and_breaks_chains(self, runner):
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        runner._skip_cancel_ts[TKR] = time.time()
        params = runner.params_by_ticker[TKR]
        st = runner._accrual_for(TKR, params); st.last_ts = T0; st.last_share = 0.5
        assert runner.pull_all_exposure("ws_disconnect") == 1
        runner.qm.cancel_all.assert_called_once_with(market_ticker=TKR, only_ours=True)
        assert runner._accrual[TKR].last_ts is None


# ── 2. High: actual remaining quantities ─────────────────────────────────

class _FakeClient:
    def __init__(self, orders):
        self.orders = orders

    def get(self, path, params=None):
        assert path == "/portfolio/orders"
        return {"orders": list(self.orders), "cursor": None}


def _venue(order_id, side="yes", price="0.5000", remaining="25.50", coid="LIP-abc", status="resting",
           ticker=TKR):
    return {"order_id": order_id, "ticker": ticker, "side": side, "status": status,
            f"{side}_price_dollars": price, "remaining_count_fp": remaining,
            "initial_count_fp": "100.00", "client_order_id": coid,
            "created_time": "2026-09-20T12:00:00Z"}


@pytest.fixture
def live_qm(db):
    qm = QuoteManager(paper=True, db_path=db)
    qm.paper = False
    return qm


class TestResyncRemainingQuantity:
    def test_reviewer_reproduction_partial_fill(self, live_qm):
        """Venue remaining 25.50, local 100 → local must become 25.5."""
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=False)]
        live_qm.client = _FakeClient([_venue("o1", remaining="25.50")])
        res = live_qm.periodic_resync()
        assert res["updated"] == 1
        assert live_qm.resting[TKR][0].size_contracts == pytest.approx(25.5)

    def test_fully_filled_on_venue_is_purged(self, live_qm):
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=False)]
        live_qm.client = _FakeClient([_venue("o1", remaining="0.00")])
        live_qm.periodic_resync()
        assert TKR not in live_qm.resting

    def test_phantom_purged_and_missing_adopted(self, live_qm):
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="gone", paper=False)]
        live_qm.client = _FakeClient([_venue("new", side="no", remaining="12.25", coid="LIP-zzz")])
        res = live_qm.periodic_resync()
        assert res["phantoms_purged"] == 1 and res["added"] == 1
        (o,) = live_qm.resting[TKR]
        assert o.order_id == "new" and o.side == "no"
        assert o.size_contracts == pytest.approx(12.25) and o.is_ours

    def test_adopted_foreign_order_is_not_ours(self, live_qm):
        live_qm.client = _FakeClient([_venue("man", coid="")])
        live_qm.periodic_resync()
        assert not live_qm.resting[TKR][0].is_ours

    def test_price_change_and_off_grid_flag(self, live_qm):
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=False)]
        live_qm.client = _FakeClient([_venue("o1", price="0.4950", remaining="100.00")])
        live_qm.periodic_resync()
        o = live_qm.resting[TKR][0]
        assert o.price_off_grid and o.price_cents in (49, 50)
        live_qm.client = _FakeClient([_venue("o1", price="0.4900", remaining="100.00")])
        live_qm.periodic_resync()
        assert o.price_cents == 49 and not o.price_off_grid

    def test_legacy_integer_fields_accepted(self, live_qm):
        raw = {"order_id": "o9", "ticker": TKR, "side": "yes", "status": "resting",
               "yes_price": 43, "remaining_count": 7}
        o = QuoteManager._parse_live_order(raw)
        assert o.price_cents == 43 and o.size_contracts == 7.0

    def test_scorer_uses_corrected_remaining(self, runner, db):
        """After resync the scorer must see 25.5, not 100."""
        runner.qm.paper = False
        runner.qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=False),
                                  _order("no", 50, 100, oid="o2", paper=False)]
        runner.qm.client = _FakeClient([_venue("o1", remaining="30.00"),
                                        _venue("o2", side="no", remaining="30.00")])
        runner.qm.periodic_resync()
        s = runner._score_market(_book(yes=[(50, 60)], no=[(50, 60)]), runner.params_by_ticker[TKR])
        assert s.result.our_yes_normalized == pytest.approx(30 / 60)


class TestFillEvents:
    def test_apply_fill_partial_then_complete(self, live_qm):
        live_qm.resting[TKR] = [_order("yes", 50, 100, oid="o1", paper=False)]
        o = live_qm.apply_fill("o1", TKR, 74.5)
        assert o.size_contracts == pytest.approx(25.5)
        assert live_qm.apply_fill("o1", TKR, 25.5) is not None
        assert TKR not in live_qm.resting

    def test_apply_fill_unknown_order_ignored(self, live_qm):
        assert live_qm.apply_fill("nope", TKR, 1.0) is None

    def test_ws_parses_v2_and_legacy_fill_payloads(self):
        ev = KalshiWS._parse_fill({"order_id": "o1", "market_ticker": TKR, "side": "yes",
                                   "count_fp": "12.50", "yes_price_dollars": "0.4900",
                                   "is_taker": False, "trade_id": "t1"})
        assert (ev.order_id, ev.count, ev.price_cents_exact, ev.is_taker) == ("o1", 12.5, 49.0, False)
        ev = KalshiWS._parse_fill({"order_id": "o2", "ticker": TKR, "side": "no",
                                   "count": 3, "no_price": 40})
        assert ev.count == 3.0 and ev.price_cents_exact == 40.0
        assert KalshiWS._parse_fill({"order_id": "o3", "ticker": TKR, "count_fp": "0"}) is None

    def test_fill_channel_dispatches(self):
        ws = _ws()
        got = []

        async def cb(ev):
            got.append(ev.order_id)
        ws.on_fill(cb)
        asyncio.run(ws._handle_message(json.dumps(
            {"type": "fill", "sid": 9, "seq": 1,
             "msg": {"order_id": "o1", "market_ticker": TKR, "side": "yes", "count_fp": "1.00"}})))
        assert got == ["o1"]


# ── 3. High: price precision ─────────────────────────────────────────────

class TestOffGridPrices:
    def test_reviewer_reproduction_levels_not_collapsed(self):
        off = []
        out = KalshiWS._parse_book_side([["0.4950", "10"], ["0.5040", "5"], ["0.5000", "7"]], off)
        assert [(l.price_cents, l.size) for l in out] == [(50, 7.0)]      # only the on-grid level
        assert off == ["0.4950", "0.5040"]
        assert KalshiWS._price_to_cents("0.4950") is None
        assert KalshiWS._price_to_cents("0.5040") is None
        assert KalshiWS._price_to_cents_exact("0.4950") == pytest.approx(49.5)
        assert KalshiWS._price_to_cents("0.4900") == 49

    def test_snapshot_flags_unsupported_and_clears_on_clean_snapshot(self):
        ws = _ws()
        ws.books[TKR] = BookState(market_ticker=TKR)
        asyncio.run(ws._handle_message(_snap(seq=1, yes=[["0.4950", "10"], ["0.5000", "7"]])))
        b = ws.books[TKR]
        assert b.unsupported_grid and not b.is_usable() and b.off_grid_count == 1
        asyncio.run(ws._handle_message(_snap(sid=8, seq=1, yes=[["0.5000", "7"]])))
        assert not b.unsupported_grid and b.is_usable()

    def test_delta_with_off_grid_price_flags_and_does_not_merge(self):
        ws = _ws()
        ws.books[TKR] = BookState(market_ticker=TKR)
        seen = []

        async def cb(book):
            seen.append(book.unsupported_grid)
        ws.on_update(cb)

        async def go():
            await ws._handle_message(_snap(seq=1, yes=[["0.5000", "7"]]))
            await ws._handle_message(_delta(seq=2, price="0.5040", delta="100"))
            await ws._handle_message(_delta(seq=3, price="0.5000", delta="1"))   # dropped: unusable book? no — applied, but flag stays
        asyncio.run(go())
        b = ws.books[TKR]
        assert b.unsupported_grid
        assert [(l.price_cents, l.size) for l in b.yes_bids] == [(50, 8.0)]   # 100 never merged into 50¢
        assert seen[:2] == [False, True]                                     # consumers told once at transition

    def test_runner_refuses_to_quote_unsupported_grid(self, runner):
        book = _book(yes=[(50, 60)], no=[(50, 60)]); book.unsupported_grid = True
        assert runner._quote_target_for(book) is None
        assert runner._skip_reason[TKR] == "unsupported_grid"


# ── 4. High: interval accounting ─────────────────────────────────────────

RATE = 700.0 / (7 * 86400)


def _scored(runner, share):
    s = MagicMock()
    s.share = share
    s.is_resting = share > 0
    s.raw_our_score = 0.0
    r = MagicMock()
    r.snapshot_valid = share > 0; r.yes_qualified = r.no_qualified = share > 0
    r.yes_total_qualifying_score = r.no_total_qualifying_score = 60.0
    s.result = r
    return s


class TestForwardIntervalAccounting:
    def test_row_credits_previous_share_not_current(self, runner, db):
        params = runner.params_by_ticker[TKR]
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(0.5, T0)            # nothing known before
        p(0.0, T0 + 5)        # interval [T0, T0+5) was at 0.5
        p(0.9, T0 + 10)       # interval [T0+5, T0+10) was at 0.0
        p(0.9, T0 + 15)       # interval [T0+10, T0+15) was at 0.9
        rows = _rows(db)
        assert [r[0] for r in rows] == pytest.approx([0.0, 0.5 * RATE * 5, 0.0, 0.9 * RATE * 5])

    def test_gap_beyond_max_claims_nothing_and_restarts_chain(self, runner, db):
        params = runner.params_by_ticker[TKR]
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(1.0, T0)
        p(1.0, T0 + PaperRunner.SNAPSHOT_MAX_INTERVAL_SEC + 1)   # unobserved → 0
        p(1.0, T0 + PaperRunner.SNAPSHOT_MAX_INTERVAL_SEC + 6)   # chain restarted → 5s at 1.0
        rows = _rows(db)
        assert rows[1][0] == 0.0
        assert rows[2][0] == pytest.approx(1.0 * RATE * 5)
        assert runner._accrual[TKR].breaks == 1

    def test_break_on_unknown_state(self, runner, db):
        params = runner.params_by_ticker[TKR]
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(1.0, T0)
        runner._break_accrual(TKR, "stale_book")
        p(1.0, T0 + 5)
        assert _rows(db)[1][0] == 0.0

    def test_cancel_makes_share_known_zero(self, runner, db):
        params = runner.params_by_ticker[TKR]
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(1.0, T0)
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        runner._handle_skip(TKR, "fair_value")          # cancels → _note_flat
        st = runner._accrual[TKR]
        assert st.last_share == 0.0 and st.last_ts is not None
        st.last_ts = T0 + 2                              # pin the cancel time for determinism
        p(0.0, T0 + 5)
        assert _rows(db)[1][0] == 0.0                    # nothing credited after the cancel

    def test_clipped_to_program_window(self, runner, db):
        params = runner.params_by_ticker[TKR]
        params.start_ts, params.end_ts = T0 - 100, T0 + 2   # program ends 2s after first row
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(1.0, T0)
        p(1.0, T0 + 5)
        assert _rows(db)[1][0] == pytest.approx(1.0 * RATE * 2)
        p(1.0, T0 + 10)
        assert _rows(db)[2][0] == 0.0                    # entirely outside the window

    def test_not_credited_before_program_start(self, runner, db):
        params = runner.params_by_ticker[TKR]
        params.start_ts, params.end_ts = T0 + 3, T0 + 1000
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(1.0, T0)
        p(1.0, T0 + 5)
        assert _rows(db)[1][0] == pytest.approx(1.0 * RATE * 2)

    def test_cumulative_program_cap(self, runner, db):
        params = runner.params_by_ticker[TKR]
        params.period_seconds = 12.5          # pool 700 over 12.5s ⇒ a 5s row at share 1 = 280
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(1.0, T0)
        for i in range(1, 6):
            p(1.0, T0 + 5 * i)
        rows = _rows(db)
        assert [r[0] for r in rows] == pytest.approx([0.0, 280.0, 280.0, 140.0, 0.0, 0.0])
        assert sum(r[0] for r in rows) == pytest.approx(700.0)      # never exceeds the pool
        assert runner._accrual[TKR].accrued_usd == pytest.approx(700.0)

    def test_account_share_cap_knob(self, runner, db, monkeypatch):
        monkeypatch.setattr(settings, "LIP_MAX_ACCOUNT_SHARE_OF_POOL", 0.5)
        params = runner.params_by_ticker[TKR]
        params.period_seconds = 12.5
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(1.0, T0)
        for i in range(1, 5):
            p(1.0, T0 + 5 * i)
        rows = _rows(db)
        assert [r[0] for r in rows] == pytest.approx([0.0, 280.0, 70.0, 0.0, 0.0])
        assert sum(r[0] for r in rows) == pytest.approx(350.0)

    def test_cap_seeded_from_existing_rows(self, runner, db):
        params = runner.params_by_ticker[TKR]
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO lip_snapshots (market_ticker, captured_at, our_score, total_score, "
                     "snapshot_valid, estimated_payout_usd) VALUES (?, '2027-01-14T01:00:00+00:00', 0, 0, 1, ?)",
                     (TKR, 699.0))
        conn.commit(); conn.close()
        p = lambda share, t: runner._persist_snapshot(TKR, _scored(runner, share), params, t)
        p(1.0, T0); p(1.0, T0 + 5); p(1.0, T0 + 10)
        assert runner._accrual[TKR].accrued_usd <= 700.0 + 1e-9
        rows = _rows(db)
        assert sum(r[0] for r in rows[1:]) <= 1.0 + 1e-9

    def test_new_program_window_resets_accrued(self, runner, db):
        params = runner.params_by_ticker[TKR]
        st = runner._accrual_for(TKR, params); st.accrued_usd = 500.0
        params2 = ProgramParams(TKR, 50, 0.5, 700.0, 7 * 86400, start_ts=params.start_ts + 7 * 86400)
        assert runner._accrual_for(TKR, params2).accrued_usd == 0.0

    def test_program_params_carry_window(self):
        p = _program_params_from_market(_market())
        assert p.start_ts == pytest.approx(T0 - 32 * 3600)     # 2027-01-14T00:00:00Z
        assert p.end_ts == pytest.approx(p.start_ts + 7 * 86400)
        assert p.start_ts <= T0 < p.end_ts

    def test_post_reconcile_share_applied_forward(self, runner, db):
        """A reprice at t changes the state; the share credited over
        [t, next) must be the post-reconcile one."""
        runner.qm.paper = False
        params = runner.params_by_ticker[TKR]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        runner._quote_target_for = MagicMock(return_value=MagicMock(
            market_ticker=TKR, yes_bid_cents=50, no_bid_cents=50, size_contracts=30,
            yes_size_override=None, no_size_override=None))

        def reconcile(target):
            runner.qm.resting[TKR] = [_order("yes", 50, 30, paper=False), _order("no", 50, 30, paper=False)]
            return {"cancelled": 0, "placed": 2, "kept": 0}
        runner.qm.reconcile = reconcile
        runner.last_score_ts[TKR] = 0
        asyncio.run(runner.on_book_update(book))
        st = runner._accrual[TKR]
        assert st.last_share == pytest.approx(0.5)      # not the phantom 0.0 persisted before placement


# ── 5. Reopened: qualification, retirement, interlock ────────────────────

class TestQualificationAwarePlacement:
    def test_thin_side_topped_up_to_cliff(self, runner):
        p = runner.params_by_ticker[TKR]
        book = _book(yes=[(50, 60)], no=[(50, 10)])          # NO side 10 < target 50
        reason, yes_ov, no_ov = runner._qualification_adjust(book, p, 50, 50, 25, None, None)
        assert reason is None and yes_ov is None and no_ov == 40

    def test_gap_beyond_cap_is_unqualifiable(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "MAX_GROSS_PER_MARKET_USD", 10.0)
        monkeypatch.setattr(settings, "MAX_GROSS_PER_MARKET_BY_SERIES", {})
        p = runner.params_by_ticker[TKR]
        book = _book(yes=[(50, 60)], no=[(50, 5)])
        reason, _, _ = runner._qualification_adjust(book, p, 50, 50, 25, None, None)
        assert reason == "unqualifiable:no_depth"

    def test_price_beyond_cutoff_is_unqualifiable(self, runner):
        p = runner.params_by_ticker[TKR]
        book = _book(yes=[(50, 60)], no=[(50, 60)])
        reason, _, _ = runner._qualification_adjust(book, p, 49, 50, 25, None, None)
        assert reason == "unqualifiable:yes_beyond_cutoff"

    def test_live_mode_subtracts_own_depth_before_checking(self, runner):
        runner.qm.paper = False
        p = runner.params_by_ticker[TKR]
        runner.qm.resting[TKR] = [_order("no", 50, 30, paper=False)]
        book = _book(yes=[(50, 60)], no=[(50, 40)])          # 40 includes our 30 ⇒ public 10
        reason, _, no_ov = runner._qualification_adjust(book, p, 50, 50, 25, None, None)
        assert reason is None and no_ov == 40

    def test_unqualifiable_skip_pulls_orders(self, runner, monkeypatch):
        monkeypatch.setattr(settings, "MAX_GROSS_PER_MARKET_USD", 10.0)
        monkeypatch.setattr(settings, "MAX_GROSS_PER_MARKET_BY_SERIES", {})
        runner.qm.resting[TKR] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        book = _book(yes=[(50, 60)], no=[(50, 5)])
        assert runner._quote_target_for(book) is None
        assert runner._skip_reason[TKR].startswith("unqualifiable")
        assert runner._handle_skip(TKR, runner._skip_reason[TKR])


class TestRetirement:
    def test_inactive_program_retired_and_exposure_pulled(self, runner, db):
        gone = "KXGONE-26SEP30-T1"
        runner.params_by_ticker[gone] = _program_params_from_market(_market(market_ticker=gone))
        runner.markets.append(_market(market_ticker=gone))
        runner.qm.resting[gone] = [_order("yes", 50, MIN), _order("no", 50, MIN)]
        conn = sqlite3.connect(db)
        conn.executemany(
            "INSERT INTO lip_programs (id, market_ticker, series_ticker, start_date, end_date, "
            "period_reward_usd, discount_factor, target_size, paid_out, enrolled, reward_per_day_usd, last_seen) "
            "VALUES (?, ?, 'KXTEST', ?, ?, 700, 0.5, 50, ?, 1, 100, 'x')",
            [(TKR, TKR, "2026-01-01", "2099-01-01", 0),
             (gone, gone, "2026-01-01", "2099-01-01", 1)])   # paid out
        conn.commit(); conn.close()
        retired = runner.retire_inactive_markets(db)
        assert retired == [gone]
        assert gone not in runner.params_by_ticker
        assert all(m["market_ticker"] != gone for m in runner.markets)
        runner.qm.cancel_all.assert_called_once_with(market_ticker=gone, only_ours=True)

    def test_query_failure_retires_nothing(self, runner, tmp_path):
        assert runner.retire_inactive_markets(str(tmp_path / "missing.db")) == []
        assert TKR in runner.params_by_ticker


class TestLiveInterlock:
    def test_live_without_ack_is_forced_to_paper(self, monkeypatch, db):
        monkeypatch.setattr(settings, "LIVE_ARMED", False)
        qm = QuoteManager(paper=False, db_path=db)
        assert qm.paper and qm.client is None

    def test_settings_default_is_paper(self):
        assert settings.PAPER_MODE is True
        assert settings.LIVE_ARMED is False
