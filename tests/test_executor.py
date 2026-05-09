"""Tests for engine/executor.py — sizing, paper trades, guards."""

import sqlite3
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine.executor import Executor

TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_exec.db")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    monkeypatch.setattr("engine.executor.DB_PATH", TEST_DB)
    monkeypatch.setattr("engine.executor.SHADOW_MODE", False)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY, opportunity_id INTEGER,
            kalshi_market_id TEXT, term TEXT, side TEXT, order_type TEXT,
            price REAL, contracts INTEGER, total_cost REAL, fee_paid REAL,
            paper BOOLEAN DEFAULT TRUE, placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP, outcome TEXT, pnl REAL
        );
        DELETE FROM trades;
    """)
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_size_position_basic():
    ex = Executor(bankroll=100.0)
    contracts = ex.size_position(kelly_fraction=0.10, price=0.50)
    # 100 * 0.10 / 0.50 = 20
    assert contracts == 20


def test_size_position_capped():
    from config.settings import MAX_TRADE_PCT
    ex = Executor(bankroll=1000.0)
    contracts = ex.size_position(kelly_fraction=0.50, price=0.10)
    # Capped at MAX_TRADE_PCT (15%) → 1000 * 0.15 / 0.10 = 1500
    assert contracts == int(1000 * MAX_TRADE_PCT / 0.10)


def test_size_position_below_min():
    from config.settings import MIN_CONTRACTS
    ex = Executor(bankroll=1.0)  # tiny bankroll
    contracts = ex.size_position(kelly_fraction=0.10, price=0.50)
    # 1.0 * 0.10 / 0.50 = 0.2 → 0 contracts
    assert contracts == 0


def test_paper_trade_logs_to_db():
    ex = Executor(bankroll=100.0)
    ex.paper = True
    trade = ex.place_limit_order(
        ticker="KXTEST-123", term="test_term", side="yes",
        price_cents=50, contracts=10
    )
    assert trade["paper"] is True
    assert trade["contracts"] == 10

    # Verify DB entry
    conn = sqlite3.connect(TEST_DB)
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade["trade_id"],)).fetchone()
    conn.close()
    assert row is not None


def test_live_trade_raises_on_api_error():
    ex = Executor(bankroll=100.0)
    ex.paper = False
    with pytest.raises(Exception):
        ex.place_limit_order(
            ticker="KXTEST-FAKE-123", term="test_term", side="yes",
            price_cents=50, contracts=10, paper=False
        )


def test_execute_opportunity():
    ex = Executor(bankroll=200.0)
    ex.paper = True
    result = ex.execute_opportunity({
        "ticker": "KXTEST-OPP",
        "term": "inflation",
        "trade_side": "yes",
        "market_price": 0.50,
        "kelly_fraction": 0.10,
    })
    assert not result.get("skipped")
    assert result["contracts"] > 0
