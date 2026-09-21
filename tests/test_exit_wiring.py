"""The exit policy, wired into the runner (2026-09-21).

engine/inventory_exit.py existed and was tested, but nothing called it.
run_paper.py had no liquidate/unwind path at all, so inventory a maker
never wanted was held to settlement. These tests pin the wiring, and in
particular the separations the policy alone cannot enforce:

  * entry eligibility != inventory-reduction eligibility. A retired or
    expired market still gets its position managed;
  * a conflicting entry order on the reducing side is cancelled FIRST,
    otherwise the "reduction" adds size to the wrong side;
  * the action is re-checked against the position as it exists AFTER that
    cancel, because a fill can land in between.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_ws import BookLevel, BookState
from execution.quote_manager import RestingOrder
import run_paper as rp
from run_paper import PaperRunner

TKR = "KXEXIT-26SEP30-T1"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "exit.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE lip_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_ticker TEXT, captured_at TEXT,
            our_score REAL, total_score REAL, yes_qualified INTEGER, no_qualified INTEGER,
            snapshot_valid INTEGER, estimated_payout_usd REAL, was_resting INTEGER,
            our_share REAL);
        CREATE TABLE fill_ledger (
            trade_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT, side TEXT,
            count INTEGER, yes_price_cents INTEGER, no_price_cents INTEGER,
            is_taker INTEGER, created_at TEXT, synced_at TEXT);
        CREATE TABLE settlement_log (ticker TEXT PRIMARY KEY);
        CREATE TABLE quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_ticker TEXT, side TEXT,
            price_cents INTEGER, size_contracts INTEGER, order_id TEXT, status TEXT,
            paper INTEGER, placed_at TEXT, cancelled_at TEXT, filled_at TEXT,
            fill_size INTEGER, fill_price_cents INTEGER, notes TEXT);
    """)
    conn.commit(); conn.close()
    monkeypatch.setattr(settings, "DB_PATH", str(path))
    return str(path)


def _market(**kw):
    m = dict(id="P-exit", market_ticker=TKR, target_size=50, discount_factor=0.5,
             reward_per_day_usd=100.0, period_reward_usd=700.0,
             period_seconds=7 * 86400.0, start_date="2026-01-01T00:00:00Z",
             end_date="2028-01-01T00:00:00Z")
    m.update(kw)
    return m


def _fill(db_path, side, count, age_sec, ticker=TKR):
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO fill_ledger (trade_id, order_id, ticker, side, count,"
        " yes_price_cents, no_price_cents, is_taker, created_at, synced_at)"
        " VALUES (?,?,?,?,?,?,?,0,?,?)",
        (f"t-{side}-{count}-{age_sec}-{ticker}", "o1", ticker, side, count,
         50, 50, ts, ts))
    conn.commit(); conn.close()


@pytest.fixture
def runner(db, tmp_path):
    r = PaperRunner([_market()])
    r.qm.paper = True
    r.qm.db_path = db
    r.qm.resting = {}
    book = BookState(market_ticker=TKR)
    book.yes_bids = [BookLevel(45, 500.0)]
    book.no_bids = [BookLevel(50, 500.0)]
    book.snapshot_count = 1
    r.books = {TKR: book}
    return r


# ── position construction ─────────────────────────────────────────────────

def test_position_reflects_paired_and_net(runner, db):
    _fill(db, "yes", 30, 10)
    _fill(db, "no", 10, 10)
    pos = runner._position_for(TKR)
    assert pos.yes_qty == Decimal("30") and pos.no_qty == Decimal("10")
    assert pos.net_yes == Decimal("20")
    assert pos.paired == Decimal("10")          # riskless, but capital locked


def test_flat_market_has_no_position(runner, db):
    _fill(db, "yes", 10, 10)
    _fill(db, "no", 10, 10)
    pos = runner._position_for(TKR)
    assert pos.net_yes == 0


# ── the separation that matters ───────────────────────────────────────────

def test_inventory_is_managed_after_the_market_is_retired(runner, db):
    """Retiring stops quoting. It must not abandon the position."""
    _fill(db, "yes", 40, 10_000)                 # old and large
    runner.retire_market(TKR, "program_ended")
    assert TKR not in runner.params_by_ticker    # no longer quoted
    assert TKR in runner._exit_watch             # still managed
    out = runner.manage_exits(time.time())
    assert out["checked"] == 1
    assert out["exits"] == 1, "a retired market's inventory was never unwound"


def test_exit_buys_the_opposite_side_to_reduce_net(runner, db):
    _fill(db, "yes", 40, 10_000)
    placed = []
    runner.qm._place_order = lambda t, side, px, qty, **kw: placed.append(
        (t, side, px, qty)) or RestingOrder(
            order_id="x", market_ticker=t, side=side, price_cents=px,
            size_contracts=float(qty), placed_at=time.time())
    runner.manage_exits(time.time())
    assert placed and placed[0][1] == "no", "long YES was not reduced by buying NO"


def test_within_limits_does_not_exit(runner, db):
    _fill(db, "yes", 5, 10)                      # young and small
    out = runner.manage_exits(time.time())
    assert out["exits"] == 0 and out["skipped"] == 1


# ── conflicting orders and intervening fills ──────────────────────────────

def test_conflicting_entry_order_is_cancelled_before_reducing(runner, db):
    _fill(db, "yes", 40, 10_000)
    entry = RestingOrder(order_id="e1", market_ticker=TKR, side="no",
                         price_cents=50, size_contracts=20.0,
                         placed_at=time.time(), paper=True,
                         client_order_id="LIP-e1")
    runner.qm.resting = {TKR: [entry]}
    cancelled = []
    runner.qm._cancel_order = lambda o: cancelled.append(o.order_id) or True
    runner.qm._place_order = lambda *a, **k: RestingOrder(
        order_id="x", market_ticker=TKR, side="no", price_cents=50,
        size_contracts=1.0, placed_at=time.time())
    runner.manage_exits(time.time())
    assert cancelled == ["e1"], \
        "an entry order on the reducing side was left resting"


def test_exit_aborts_if_the_position_flattened_in_between(runner, db):
    """A fill can land between the decision and the action. The action must
    be re-checked against the position as it is NOW."""
    _fill(db, "yes", 40, 10_000)
    calls = {"n": 0}
    real = runner._position_for

    def flaky(ticker):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(ticker)
        _fill(db, "no", 40, 5)                  # offsetting fill arrives
        return real(ticker)

    runner._position_for = flaky
    runner.qm._place_order = MagicMock()
    out = runner.manage_exits(time.time())
    assert out["exits"] == 0 and out["blocked"] == 1
    runner.qm._place_order.assert_not_called()
    assert runner._exit_reasons[TKR] == "flattened_before_exit"


def test_exit_never_increases_exposure(runner, db):
    """reduces_exposure is checked, not assumed."""
    from engine.inventory_exit import Position, reduces_exposure
    pos = Position(market_ticker=TKR, yes_qty=Decimal("40"),
                   no_qty=Decimal("0"), oldest_fill_ts=0.0)
    assert reduces_exposure(pos, "no", Decimal("10")) is True
    assert reduces_exposure(pos, "yes", Decimal("10")) is False
