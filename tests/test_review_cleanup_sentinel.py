"""Review (2026-10-01): risk/sentinel.py paper bypass and fail-closed reads.

1. The paper bypass read settings.LIP_PAPER, which config.settings never
   defines (it defines PAPER_MODE), so the bypass never fired.
2. _daily_realized_pnl() and _inventory() swallowed every exception and
   returned 0 / {} — an unreadable loss or inventory looked like "no loss,
   no exposure" and the order was approved. The module docstring promises
   fail-closed; a read failure must now veto.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from config import constitution, settings
from risk.sentinel import Sentinel


@dataclass
class T:
    market_ticker: str = "KXREVIEW-26OCT01-T1"
    yes_bid_cents: int = 50
    no_bid_cents: int = 50
    size_contracts: int = 100
    yes_size_override: int = 0
    no_size_override: int = 0


@pytest.fixture
def live_mode(monkeypatch):
    from mm.risk import FILL_CLOCK
    Sentinel.reset_rate_clock()
    FILL_CLOCK.reset()
    monkeypatch.setattr(settings, "PAPER_MODE", False)
    monkeypatch.setattr(settings, "BANKROLL_USD", 1000.0)
    monkeypatch.setattr(settings, "RAMP_PHASE", 4)
    yield
    Sentinel.reset_rate_clock()


def _db(tmp_path, *, pnl=True, inventory=True):
    path = str(tmp_path / "s.db")
    conn = sqlite3.connect(path)
    if pnl:
        conn.execute("CREATE TABLE daily_pnl_log (day TEXT PRIMARY KEY, "
                     "daily_realized_delta REAL, snapshot_at TEXT)")
    if inventory:
        conn.execute("CREATE TABLE inventory (market_ticker TEXT PRIMARY KEY, "
                     "net_yes_contracts INTEGER, gross_usd REAL)")
    conn.commit()
    conn.close()
    return path


def test_paper_bypass_reads_paper_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(constitution, "PAPER_BYPASS", True)
    monkeypatch.setattr(settings, "PAPER_MODE", True)
    # A thin bid that the live checks would veto.
    ok, reason = Sentinel(_db(tmp_path)).approve(T(yes_bid_cents=2))
    assert (ok, reason) == (True, "paper_bypass")


def test_no_bypass_when_not_paper(tmp_path, monkeypatch, live_mode):
    monkeypatch.setattr(constitution, "PAPER_BYPASS", True)
    ok, reason = Sentinel(_db(tmp_path)).approve(T(yes_bid_cents=2))
    assert not ok and "thin_bid" in reason


def test_clean_state_still_approves(tmp_path, live_mode):
    ok, reason = Sentinel(_db(tmp_path)).approve(T())
    assert ok, reason


def test_unreadable_daily_pnl_fails_closed(tmp_path, live_mode):
    ok, reason = Sentinel(_db(tmp_path, pnl=False)).approve(T())
    assert not ok and reason.startswith("sentinel_error"), reason


def test_unreadable_inventory_fails_closed(tmp_path, live_mode):
    ok, reason = Sentinel(_db(tmp_path, inventory=False)).approve(T())
    assert not ok and reason.startswith("sentinel_error"), reason
