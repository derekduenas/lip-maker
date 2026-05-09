"""Tests for loop/reviewer.py — resolution, scoring, lessons."""

import sqlite3
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loop.reviewer import manual_resolve, score_strategies, _fallback_lesson

TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_review.db")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    monkeypatch.setattr("loop.reviewer.DB_PATH", TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY, opportunity_id INTEGER,
            kalshi_market_id TEXT, term TEXT, side TEXT, order_type TEXT,
            price REAL, contracts INTEGER, total_cost REAL, fee_paid REAL,
            paper BOOLEAN DEFAULT TRUE, placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP, outcome TEXT, pnl REAL
        );
        CREATE TABLE IF NOT EXISTS strategy_scores (
            id INTEGER PRIMARY KEY, strategy TEXT, trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0, avg_roi REAL,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY, kalshi_market_id TEXT, ticker TEXT,
            term TEXT, event_type TEXT, event_date DATE,
            yes_price REAL, volume INTEGER,
            snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        DELETE FROM trades;
        DELETE FROM strategy_scores;
    """)
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def _insert_trade(term, side, price, contracts, outcome="OPEN"):
    conn = sqlite3.connect(TEST_DB)
    conn.execute(
        """INSERT INTO trades (kalshi_market_id, term, side, order_type, price,
           contracts, total_cost, fee_paid, paper, outcome)
           VALUES (?, ?, ?, 'LIMIT', ?, ?, ?, 0.01, 1, ?)""",
        (f"KX-{term}", term, side, price, contracts, price * contracts, outcome)
    )
    conn.commit()
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return tid


def test_manual_resolve_win():
    tid = _insert_trade("inflation", "YES", 0.50, 10)
    result = manual_resolve(tid, "yes")
    assert result["outcome"] == "WIN"
    assert result["pnl"] > 0


def test_manual_resolve_loss():
    tid = _insert_trade("recession", "YES", 0.60, 10)
    result = manual_resolve(tid, "no")
    assert result["outcome"] == "LOSS"
    assert result["pnl"] < 0


def test_manual_resolve_no_side_win():
    tid = _insert_trade("stagflation", "NO", 0.80, 10)
    result = manual_resolve(tid, "no")
    assert result["outcome"] == "WIN"


def test_score_strategies_after_resolve():
    t1 = _insert_trade("a", "YES", 0.40, 10, outcome="WIN")
    t2 = _insert_trade("b", "YES", 0.50, 10, outcome="LOSS")

    # Set PnL manually
    conn = sqlite3.connect(TEST_DB)
    conn.execute("UPDATE trades SET pnl=6.0 WHERE id=?", (t1,))
    conn.execute("UPDATE trades SET pnl=-5.0 WHERE id=?", (t2,))
    conn.commit()
    conn.close()

    score_strategies()

    conn = sqlite3.connect(TEST_DB)
    row = conn.execute("SELECT trades, wins, losses, total_pnl FROM strategy_scores").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 2  # total trades
    assert row[1] == 1  # wins
    assert row[2] == 1  # losses


def test_fallback_lesson_win():
    trade = {"term": "inflation", "side": "YES", "price": 0.50, "outcome": "WIN", "pnl": 5.0, "cost": 5.0}
    lesson = _fallback_lesson(trade)
    assert "inflation" in lesson
    assert "correct" in lesson.lower()


def test_fallback_lesson_loss():
    trade = {"term": "recession", "side": "YES", "price": 0.60, "outcome": "LOSS", "pnl": -6.0, "cost": 6.0}
    lesson = _fallback_lesson(trade)
    assert "recession" in lesson
    assert "lost" in lesson.lower()
