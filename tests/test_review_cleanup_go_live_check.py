"""Review (2026-10-01): false-pass holes in tools/go_live_check.py.

1. Drawdown: the equity curve's peak started at the first day's equity, so
   a first-day loss was never counted as drawdown.
2. Sharpe: computed over days WITH settlements only; idle calendar days
   (zero P&L) were dropped, inflating mean / sd.
3. Basis residual: |mean(residual)| let +900 / -900 days cancel to 0; the
   gate now uses mean(|residual|).
4. Paid-reward evidence: counted payments from any time, not the window
   the other gates look at.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import tools.go_live_check as gc


def _db(tmp_path):
    path = str(tmp_path / "gl.db")
    with sqlite3.connect(path) as c:
        c.execute("""CREATE TABLE settlement_log (
            ticker TEXT, series_prefix TEXT, close_time TEXT,
            net_outcome_usd REAL, rebate_paid_usd REAL,
            reward_provenance TEXT, recorded_at TEXT)""")
        c.execute("""CREATE TABLE hedge_residual_log (
            series_prefix TEXT, residual_usd REAL, n_fills INT, window_end TEXT)""")
    return path


def _settle(path, when, net, paid=None):
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO settlement_log VALUES (?,?,?,?,?,?,?)",
                  ("KX-1", "KX", when.isoformat(), net, paid,
                   "paid" if paid else "estimate", when.isoformat()))


def _gate(rep, name):
    return next(g for g in rep.gates if g.name == name)


def test_first_day_loss_counts_as_drawdown():
    g = gc._gate_max_dd([-1000.0, 10.0, 10.0, 10.0])
    assert g.observed == pytest.approx(1000.0)
    assert not g.passed


def test_sharpe_counts_idle_calendar_days_as_zero(tmp_path):
    path = _db(tmp_path)
    now = datetime.now(timezone.utc)
    for i, v in enumerate([5, 6, 5, 7, 6]):
        _settle(path, now - timedelta(days=i, hours=1), v)
    rep = gc.run_check(db_path=path, days=14)
    g = _gate(rep, "daily_sharpe")
    # Five good days out of fourteen: zero-filled Sharpe is well under 1.0.
    assert not g.insufficient_data
    assert g.observed < gc.GATE_DAILY_SHARPE_MIN
    assert not g.passed


def test_basis_residual_does_not_cancel(tmp_path):
    path = _db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as c:
        for r in (900, -900, 880, -880):
            c.execute("INSERT INTO hedge_residual_log VALUES ('KXB', ?, 5, ?)", (r, now))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    g = gc._gate_basis_residual(path, cutoff)
    assert g.observed == pytest.approx(890.0)
    assert not g.passed


def test_paid_reward_evidence_is_windowed(tmp_path):
    path = _db(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    _settle(path, old, 1.0, paid=12.5)
    rep = gc.run_check(db_path=path, days=14)
    g = _gate(rep, "paid_reward_evidence")
    assert not g.passed and g.insufficient_data
    _settle(path, datetime.now(timezone.utc) - timedelta(days=1), 1.0, paid=3.0)
    rep = gc.run_check(db_path=path, days=14)
    g = _gate(rep, "paid_reward_evidence")
    assert g.passed and g.observed == pytest.approx(3.0)
