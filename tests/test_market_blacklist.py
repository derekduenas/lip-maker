"""Tests for execution/market_blacklist.py — pin add/check/expire logic.

Why critical: this gate fires in the safety-check hot path on every quote
attempt. is_blocked() failure = double-bleed on a bad market. The fail-open
behavior on DB error is intentional but the rest must be exact.

Run: python -m unittest tests.test_market_blacklist
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.market_blacklist import (
    add_block,
    cleanup_expired,
    init_schema,
    is_blocked,
    list_active_blocks,
)


def _fresh_db() -> str:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    f.close()
    init_schema(f.name)
    return f.name


class TestAddAndCheck(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def test_unblocked_ticker_returns_false(self):
        blocked, reason = is_blocked("KX-NEVER-BLOCKED", self.db)
        self.assertFalse(blocked)
        self.assertIsNone(reason)

    def test_add_then_check_returns_true(self):
        add_block("KX-TEST-1", "manual", hours=1, db_path=self.db)
        blocked, reason = is_blocked("KX-TEST-1", self.db)
        self.assertTrue(blocked)
        self.assertEqual(reason, "manual")

    def test_add_with_zero_hours_blocks_immediately_then_expires(self):
        # 0 hours = expires_at == now → already expired → not blocked
        add_block("KX-FAST", "test", hours=0, db_path=self.db)
        blocked, _ = is_blocked("KX-FAST", self.db)
        self.assertFalse(blocked)

    def test_extend_block_via_re_add(self):
        # Re-adding the same ticker should overwrite, not create duplicate row
        add_block("KX-EXTEND", "first", hours=1, db_path=self.db)
        add_block("KX-EXTEND", "second", hours=24, db_path=self.db)
        blocked, reason = is_blocked("KX-EXTEND", self.db)
        self.assertTrue(blocked)
        self.assertEqual(reason, "second")  # latest wins

    def test_unrelated_ticker_not_affected_by_block(self):
        add_block("KX-A", "test", hours=24, db_path=self.db)
        blocked_b, _ = is_blocked("KX-B", self.db)
        self.assertFalse(blocked_b)


class TestExpiration(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def test_expired_block_returns_false(self):
        # Manually insert an expired block
        conn = sqlite3.connect(self.db)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO market_blacklist VALUES (?, ?, ?, ?)",
            ("KX-EXPIRED", past, "old", now),
        )
        conn.commit()
        conn.close()
        blocked, _ = is_blocked("KX-EXPIRED", self.db)
        self.assertFalse(blocked)

    def test_cleanup_removes_only_expired(self):
        add_block("KX-ACTIVE", "live", hours=24, db_path=self.db)
        # Insert expired manually
        conn = sqlite3.connect(self.db)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO market_blacklist VALUES (?, ?, ?, ?)",
            ("KX-EXPIRED", past, "stale", now),
        )
        conn.commit()
        conn.close()

        deleted = cleanup_expired(self.db)
        self.assertEqual(deleted, 1)
        # Active still present
        active = list_active_blocks(self.db)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0][0], "KX-ACTIVE")


class TestFailOpenOnDbError(unittest.TestCase):
    """is_blocked is fail-OPEN — DB error → not blocked. Intentional: don't
    halt trading on transient DB issues. But pin the behavior."""

    def test_nonexistent_db_returns_unblocked(self):
        # Path doesn't exist → connect creates empty DB without table → query fails
        # → except returns (False, None). We rely on this fail-open.
        blocked, reason = is_blocked("KX-X", "/tmp/nonexistent_dir/nope.db")
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
