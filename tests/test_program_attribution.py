"""P5b — program-aware attribution.

Before this, runtime state was keyed by market_ticker: `params_by_ticker`,
`_accrual` and `_seed_accrued` all assumed one program per ticker. Two
overlapping LIP programs on the same market therefore pooled each other's
accrual history and each other's cumulative cap, and `refresh_params`
silently replaced one with the other.

The invariants pinned here:
  * both overlapping programs survive discovery and refresh;
  * each accrues independently, under its OWN target/pool/window;
  * a ticker-level event (stale book, flat) applies to every program on it;
  * seeding reads only this program's rows (plus unattributable legacy
    rows, which are conservative for a cap) — never a sibling program's;
  * one physical order set produces exactly ONE capital reservation, no
    matter how many programs it serves. That is the money invariant: a
    second program must not let the account spend the same $ twice.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine.account_ledger import AccountLedger
from engine.lip_scorer import ProgramParams
import run_paper as rp
from run_paper import PaperRunner, _program_params_from_market

TKR = "KXDUAL-26SEP30-T1"
T0 = 1_800_000_000.0


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "prog.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE lip_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_ticker TEXT, captured_at TEXT,
            our_score REAL, total_score REAL, yes_qualified INTEGER, no_qualified INTEGER,
            snapshot_valid INTEGER, estimated_payout_usd REAL, was_resting INTEGER,
            our_share REAL
        );
    """)
    conn.commit(); conn.close()
    monkeypatch.setattr(settings, "DB_PATH", str(path))
    return str(path)


def _market(pid, target, pool, **kw):
    m = dict(id=pid, market_ticker=TKR, target_size=target, discount_factor=0.5,
             reward_per_day_usd=pool, period_reward_usd=pool, period_seconds=86400.0,
             start_date="2026-01-01T00:00:00Z", end_date="2028-01-01T00:00:00Z")
    m.update(kw)
    return m


def _runner(markets, db):
    r = PaperRunner(markets)
    r.qm.paper = True
    r.qm.resting = {}
    r.qm.cancel_all = MagicMock(return_value=1)
    return r


# ── registry ──────────────────────────────────────────────────────────────

def test_two_programs_on_one_ticker_both_survive(db):
    r = _runner([_market("P-small", 50, 100.0), _market("P-big", 400, 900.0)], db)
    keys = {p.program_id for p in r.programs_for(TKR)}
    assert keys == {"P-small", "P-big"}, "an overlapping program was dropped"


def test_binding_program_is_the_most_demanding_target(db):
    r = _runner([_market("P-small", 50, 100.0), _market("P-big", 400, 900.0)], db)
    # One order set serves both; it must satisfy the LARGER target, since a
    # quote meeting the big target also meets the small one.
    assert r.params_by_ticker[TKR].program_id == "P-big"


def test_refresh_params_adds_a_second_program_instead_of_replacing(db):
    r = _runner([_market("P-1", 50, 100.0)], db)
    out = r.refresh_params([_market("P-2", 400, 900.0)])
    assert out["added"] == 1
    assert {p.program_id for p in r.programs_for(TKR)} == {"P-1", "P-2"}


def test_refresh_params_still_updates_an_existing_program(db):
    r = _runner([_market("P-1", 50, 100.0)], db)
    out = r.refresh_params([_market("P-1", 50, 250.0)])
    assert out["changed"] == 1 and out["added"] == 0
    assert r.programs_by_id["P-1"].period_reward_usd == 250.0


# ── independent accrual ───────────────────────────────────────────────────

def test_accrual_state_is_per_program_not_per_ticker(db):
    r = _runner([_market("P-1", 50, 100.0), _market("P-2", 400, 900.0)], db)
    a = r._accrual_for(TKR, r.programs_by_id["P-1"])
    b = r._accrual_for(TKR, r.programs_by_id["P-2"])
    assert a is not b, "overlapping programs shared one accrual chain"
    a.accrued_usd = 5.0
    assert b.accrued_usd == 0.0, "one program's accrual leaked into the other"


def test_break_accrual_breaks_every_program_on_the_ticker(db):
    r = _runner([_market("P-1", 50, 100.0), _market("P-2", 400, 900.0)], db)
    for pid in ("P-1", "P-2"):
        st = r._accrual_for(TKR, r.programs_by_id[pid])
        st.last_ts, st.last_share = T0, 0.4
    r._break_accrual(TKR, "stale_book")
    # A stale book is unknown state for BOTH programs: neither may claim it.
    assert all(r._accrual[p].last_ts is None for p in ("P-1", "P-2"))


def test_note_flat_applies_to_every_program(db):
    r = _runner([_market("P-1", 50, 100.0), _market("P-2", 400, 900.0)], db)
    for pid in ("P-1", "P-2"):
        st = r._accrual_for(TKR, r.programs_by_id[pid])
        st.last_ts, st.last_share = T0, 0.4
    r._note_flat(TKR)
    assert all(r._accrual[p].last_share == 0.0 for p in ("P-1", "P-2"))


def test_retire_market_drops_all_programs(db):
    r = _runner([_market("P-1", 50, 100.0), _market("P-2", 400, 900.0)], db)
    r._accrual_for(TKR, r.programs_by_id["P-1"])
    r.retire_market(TKR, "ended")
    assert r.programs_for(TKR) == []
    assert not any(k in r._accrual for k in ("P-1", "P-2"))


# ── seeding: no cross-program pooling ─────────────────────────────────────

def _insert(db_path, pid, amount):
    conn = sqlite3.connect(db_path)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(lip_snapshots)").fetchall()}
    assert "program_id" in cols, "migration did not add program_id"
    conn.execute(
        "INSERT INTO lip_snapshots (market_ticker, captured_at, estimated_payout_usd,"
        " program_id) VALUES (?,?,?,?)",
        (TKR, "2026-06-01T00:00:00+00:00", amount, pid))
    conn.commit(); conn.close()


def test_seed_excludes_a_sibling_programs_rows(db):
    r = _runner([_market("P-1", 50, 100.0), _market("P-2", 400, 900.0)], db)
    _insert(db, "P-1", 3.0)
    _insert(db, "P-2", 11.0)
    p1 = r.programs_by_id["P-1"]
    p1.start_ts = None                       # seed over all history
    assert r._seed_accrued(TKR, p1) == pytest.approx(3.0), \
        "seeding pooled the sibling program's accrual"


def test_seed_counts_unattributable_legacy_rows(db):
    """Pre-P5b rows carry no program_id. They are never REPORTED as a
    program's reward, but they do count toward the cap: that direction
    claims less, and stops a migration resetting a partly-consumed cap."""
    r = _runner([_market("P-1", 50, 100.0)], db)
    _insert(db, None, 7.0)
    p1 = r.programs_by_id["P-1"]
    p1.start_ts = None
    assert r._seed_accrued(TKR, p1) == pytest.approx(7.0)


# ── the money invariant ───────────────────────────────────────────────────

def test_one_order_reserves_capital_once_regardless_of_program_count():
    """Two programs, one physical order set. Capital is reserved per
    order_id, so a second program must not reserve the same money again."""
    led = AccountLedger(opening_cash_usd=5000, mode="paper")
    led.reserve("LIP-abc", market=TKR, program_id="P-1", price_cents=40, quantity=100)
    before = led.available_usd()
    # The same physical order re-reported under the other program.
    led.reserve("LIP-abc", market=TKR, program_id="P-2", price_cents=40, quantity=100)
    assert led.available_usd() == before, "one order double-reserved the account"


def test_distinct_orders_do_consume_the_shared_account():
    led = AccountLedger(opening_cash_usd=5000, mode="paper")
    led.reserve("LIP-a", market=TKR, program_id="P-1", price_cents=40, quantity=100)
    led.reserve("LIP-b", market=TKR, program_id="P-1", price_cents=40, quantity=100)
    assert led.available_usd() == pytest.approx(5000 - 80.0)


# ── the write path ────────────────────────────────────────────────────────

def test_persist_writes_one_row_per_program_with_its_own_id(db):
    """The defect this closes: a single ticker-keyed row meant two programs
    shared one estimate. Each program must get its own row, scored under
    its own parameters."""
    from execution.kalshi_ws import BookLevel, BookState

    r = _runner([_market("P-1", 50, 100.0), _market("P-2", 400, 900.0)], db)
    book = BookState(market_ticker=TKR)
    book.yes_bids = [BookLevel(40, 500.0)]
    book.no_bids = [BookLevel(55, 500.0)]
    book.snapshot_count = 1

    binding = r.params_by_ticker[TKR]
    scored = r._score_market(book, binding, target=None)
    assert r._persist_snapshot(TKR, scored, binding, T0, book=book) is True

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT program_id FROM lip_snapshots WHERE market_ticker = ?", (TKR,)
    ).fetchall()
    conn.close()
    assert sorted(x[0] for x in rows) == ["P-1", "P-2"], \
        f"expected one row per program, got {rows}"
