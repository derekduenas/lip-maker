"""Tests for loop/daily_scan.py — daily autonomous scanning."""

import json
import os
import sqlite3
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_daily.db")


@pytest.fixture
def mock_db(monkeypatch):
    monkeypatch.setattr("loop.daily_scan.DB_PATH", TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY, source TEXT, speaker TEXT, event_type TEXT,
            event_date DATE, raw_text TEXT, word_count INTEGER,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY, opportunity_id INTEGER,
            kalshi_market_id TEXT, term TEXT, side TEXT, order_type TEXT,
            price REAL, contracts INTEGER, total_cost REAL, fee_paid REAL,
            paper BOOLEAN DEFAULT TRUE, placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP, outcome TEXT, pnl REAL
        );
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY, kalshi_market_id TEXT, term TEXT,
            base_rate REAL, context_score REAL, estimated_prob REAL,
            market_price REAL, edge REAL, kelly_fraction REAL,
            parlay_flag BOOLEAN DEFAULT FALSE,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        DELETE FROM transcripts;
        DELETE FROM trades;
    """)
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_test_run_mode_exits_cleanly():
    """--test-run should exit 0."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "loop/daily_scan.py", "--test-run"],
        capture_output=True, text=True, timeout=120,
        cwd=os.path.join(os.path.dirname(__file__), "..")
    )
    assert result.returncode == 0
    assert "pipeline healthy" in result.stdout


def test_morning_scan_returns_expected_keys(mock_db, monkeypatch):
    """morning_scan() should return a dict with expected summary keys."""
    # Mock the scanner to return empty markets (avoids API call)
    import loop.daily_scan as ds
    monkeypatch.setattr("loop.daily_scan._has_corpus", lambda et: False)

    class MockClient:
        def get_mention_markets(self):
            return []
    monkeypatch.setattr("engine.scanner.KalshiClient", MockClient)

    result = ds.morning_scan()
    assert "scanned" in result
    assert "qualified" in result
    assert "opportunities" in result
    assert "trades_placed" in result
    assert "skipped_no_corpus" in result


def test_has_corpus_false_for_unknown(mock_db):
    """_has_corpus should return False for event types with no transcripts."""
    from loop.daily_scan import _has_corpus
    assert _has_corpus("nonexistent_event_type") is False


def test_has_corpus_true_when_sufficient(mock_db):
    """_has_corpus should return True when >= 3 transcripts exist."""
    conn = sqlite3.connect(TEST_DB)
    for i in range(3):
        conn.execute(
            "INSERT INTO transcripts (source,speaker,event_type,event_date,raw_text,word_count) VALUES (?,?,?,?,?,?)",
            ("earnings", "test", "test_earnings", f"2024-0{i+1}-01", "test text", 100)
        )
    conn.commit()
    conn.close()
    from loop.daily_scan import _has_corpus
    assert _has_corpus("test_earnings") is True
