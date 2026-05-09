"""Tests for loop/monitor.py — event detection, gate check."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_get_events_needing_scan():
    from config.events import get_events_needing_scan
    events = get_events_needing_scan(within_hours=168)  # 7 days
    # Should find Tesla at minimum
    types = [e["event_type"] for e in events]
    assert "tsla_earnings" in types


def test_get_upcoming_events():
    from config.events import get_upcoming_events
    events = get_upcoming_events(within_hours=168)
    # Tesla is within 7 days
    types = [e["event_type"] for e in events]
    assert "tsla_earnings" in types


def test_get_recently_resolved():
    from config.events import get_recently_resolved
    # Nothing should have resolved in the last 48h from our event list
    events = get_recently_resolved(within_hours=48)
    # This may or may not find events depending on timing
    assert isinstance(events, list)


def test_live_gate_fails_with_no_trades(monkeypatch):
    """Gate should fail when no resolved paper trades exist."""
    import sqlite3
    test_db = os.path.join(os.path.dirname(__file__), "..", "data", "test_gate.db")
    monkeypatch.setattr("loop.monitor.DB_PATH", test_db)

    conn = sqlite3.connect(test_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY, opportunity_id INTEGER,
            kalshi_market_id TEXT, term TEXT, side TEXT, order_type TEXT,
            price REAL, contracts INTEGER, total_cost REAL, fee_paid REAL,
            paper BOOLEAN DEFAULT TRUE, placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP, outcome TEXT, pnl REAL
        );
        CREATE TABLE IF NOT EXISTS strategy_scores (
            id INTEGER PRIMARY KEY, strategy TEXT
        );
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY, edge REAL
        );
        DELETE FROM trades;
        DELETE FROM strategy_scores;
        DELETE FROM opportunities;
    """)
    conn.close()

    from loop.monitor import live_gate_check
    result = live_gate_check()
    assert result is False

    if os.path.exists(test_db):
        os.remove(test_db)
