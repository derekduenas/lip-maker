"""Tests for prep/scheduler.py — milestone determination, idempotency."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.scheduler import (
    already_done, determine_milestone, mark_done,
)


def test_milestone_t_minus_72h():
    event_time = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    now = event_time - timedelta(hours=50)  # 50h out → in T-72h window
    assert determine_milestone(event_time, now) == "T-72h"


def test_milestone_t_minus_24h():
    event_time = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    now = event_time - timedelta(hours=20)
    assert determine_milestone(event_time, now) == "T-24h"


def test_milestone_t_minus_2h():
    event_time = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    now = event_time - timedelta(hours=1)
    assert determine_milestone(event_time, now) == "T-2h"


def test_milestone_t_zero():
    """Event starting imminently → T+0 launches live monitor."""
    event_time = datetime(2026, 4, 29, 18, 30, tzinfo=timezone.utc)
    now = event_time + timedelta(minutes=10)     # 10 min after start
    assert determine_milestone(event_time, now) == "T+0"

    now = event_time - timedelta(minutes=15)     # 15 min before start
    assert determine_milestone(event_time, now) == "T+0"


def test_milestone_t_plus_24h():
    event_time = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    now = event_time + timedelta(hours=12)
    assert determine_milestone(event_time, now) == "T+24h"


def test_milestone_no_window_before_t72():
    """Too early → no milestone."""
    event_time = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    now = event_time - timedelta(hours=200)
    assert determine_milestone(event_time, now) is None


def test_milestone_no_window_after_t24():
    """Beyond review window → None."""
    event_time = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    now = event_time + timedelta(hours=100)
    assert determine_milestone(event_time, now) is None


def test_idempotency_tracking():
    """mark_done + already_done round-trip."""
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        assert already_done("ev1", "T-72h", db_path=db) is False
        mark_done("ev1", "T-72h", exit_code=0, notes="test", db_path=db)
        assert already_done("ev1", "T-72h", db_path=db) is True
        # Different milestone for same event — independent
        assert already_done("ev1", "T-24h", db_path=db) is False
    finally:
        os.unlink(db)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
