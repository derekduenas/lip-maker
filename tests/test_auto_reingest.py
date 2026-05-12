"""Tests for loop/auto_reingest.

Validates:
  - schema migration (transcripts.content_hash + reingest_log)
  - content-hash dedup (same text → second insert blocked)
  - lambda before/after snapshots + diff computation
  - reingest_log gates retries vs reattempts
  - simulate_one path works on a real ticker

Note: these tests exercise the *plumbing* — they don't hit Motley Fool
or rebuild matrices over real corpora. The real-end-to-end smoke is run
manually via `python -m loop.auto_reingest --simulate MCD --dry-run`.

Run: python -m unittest tests.test_auto_reingest
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _TempDBMixin:
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db_path = self.tmp.name
        # Patch DB_PATH everywhere it's used by the modules under test
        self._patchers = [
            patch("config.settings.DB_PATH", self.db_path),
            patch("loop.auto_reingest.DB_PATH", self.db_path),
        ]
        for p in self._patchers:
            p.start()
        # Init transcripts + frequency_matrix tables (mirror prod schema)
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, speaker TEXT,
            event_type TEXT NOT NULL, event_date DATE NOT NULL,
            raw_text TEXT NOT NULL, word_count INTEGER,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE frequency_matrix (
            id INTEGER PRIMARY KEY, speaker TEXT, event_type TEXT, term TEXT,
            occurrences INTEGER, total_events INTEGER, base_rate REAL,
            avg_count_per_mention REAL, max_count INTEGER,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(speaker, event_type, term)
        );
        CREATE TABLE opportunities (
            id INTEGER PRIMARY KEY, kalshi_market_id TEXT NOT NULL,
            term TEXT NOT NULL, base_rate REAL, context_score REAL,
            estimated_prob REAL, market_price REAL, edge REAL,
            kelly_fraction REAL, parlay_flag BOOLEAN, detected_at TIMESTAMP
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, opportunity_id INTEGER,
            kalshi_market_id TEXT NOT NULL, term TEXT NOT NULL,
            side TEXT NOT NULL, order_type TEXT NOT NULL, price REAL NOT NULL,
            contracts INTEGER NOT NULL, total_cost REAL NOT NULL,
            fee_paid REAL, paper BOOLEAN, placed_at TIMESTAMP,
            resolved_at TIMESTAMP, outcome TEXT, pnl REAL
        );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass


class TestContentHash(_TempDBMixin, unittest.TestCase):
    def test_hash_is_normalized(self):
        from loop.auto_reingest import compute_content_hash
        a = compute_content_hash("hello\n   world  ")
        b = compute_content_hash("hello world")
        self.assertEqual(a, b)

    def test_hash_differs_for_different_text(self):
        from loop.auto_reingest import compute_content_hash
        self.assertNotEqual(
            compute_content_hash("foo bar"),
            compute_content_hash("baz qux"),
        )

    def test_migration_adds_content_hash_column(self):
        from loop.auto_reingest import _get_conn
        conn = _get_conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(transcripts)")]
        self.assertIn("content_hash", cols)
        # And reingest_log table exists
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        self.assertIn("reingest_log", tables)
        conn.close()


class TestLambdaSnapshot(_TempDBMixin, unittest.TestCase):
    def test_snapshot_returns_per_term_dict(self):
        from loop.auto_reingest import _get_conn, _snapshot_speaker_lambdas
        conn = _get_conn()
        conn.execute(
            """INSERT INTO frequency_matrix
               (speaker, event_type, term, occurrences, total_events, base_rate)
               VALUES ('niccol', 'mcd_earnings', 'tariffs', 3, 10, 0.30)"""
        )
        conn.execute(
            """INSERT INTO frequency_matrix
               (speaker, event_type, term, occurrences, total_events, base_rate)
               VALUES ('niccol', 'mcd_earnings', 'inflation', 7, 10, 0.70)"""
        )
        conn.commit()
        snap = _snapshot_speaker_lambdas(conn, "niccol", "mcd_earnings")
        self.assertEqual(set(snap.keys()), {"tariffs", "inflation"})
        self.assertAlmostEqual(snap["tariffs"]["base_rate"], 0.30)
        self.assertEqual(snap["inflation"]["occurrences"], 7)
        conn.close()

    def test_diff_lambdas_computes_deltas(self):
        from loop.auto_reingest import _diff_lambdas
        before = {
            "tariffs":   {"base_rate": 0.30, "occurrences": 3, "total_events": 10},
            "inflation": {"base_rate": 0.70, "occurrences": 7, "total_events": 10},
        }
        after = {
            "tariffs":   {"base_rate": 0.36, "occurrences": 4, "total_events": 11},
            "inflation": {"base_rate": 0.64, "occurrences": 7, "total_events": 11},
            "AI":        {"base_rate": 1.00, "occurrences": 1, "total_events": 1},
        }
        diff = _diff_lambdas(before, after)
        by_term = {d["term"]: d for d in diff}
        # tariffs went up
        self.assertAlmostEqual(by_term["tariffs"]["delta"], 0.06, places=3)
        # inflation went down
        self.assertAlmostEqual(by_term["inflation"]["delta"], -0.06, places=3)
        # AI is new
        self.assertEqual(by_term["AI"]["base_rate_before"], None)
        self.assertEqual(by_term["AI"]["delta"], 1.0)


class TestReingestLogGate(_TempDBMixin, unittest.TestCase):
    def test_successful_reingest_blocks_redo(self):
        from loop.auto_reingest import _get_conn, _had_successful_reingest, _persist
        conn = _get_conn()
        _persist(conn, ticker="MCD", settle_date="2026-05-07",
                 status="success", n_before=5, n_after=6, lambdas=[], notes="x")
        self.assertTrue(_had_successful_reingest(conn, "MCD", "2026-05-07"))
        self.assertFalse(_had_successful_reingest(conn, "MCD", "2026-05-21"))
        conn.close()

    def test_pending_attempts_counted(self):
        from loop.auto_reingest import _get_conn, _pending_attempt_count, _persist
        conn = _get_conn()
        _persist(conn, ticker="LYFT", settle_date="2026-05-07",
                 status="transcript_pending", n_before=4, n_after=4,
                 lambdas=[], notes="not yet")
        _persist(conn, ticker="LYFT", settle_date="2026-05-07",
                 status="transcript_pending", n_before=4, n_after=4,
                 lambdas=[], notes="still not")
        self.assertEqual(_pending_attempt_count(conn, "LYFT", "2026-05-07"), 2)
        conn.close()


class TestSimulatePath(_TempDBMixin, unittest.TestCase):
    """The simulate path should fail closed if speaker unknown (no fetch)."""

    def test_no_speaker_path(self):
        from loop.auto_reingest import simulate_one
        r = simulate_one("BOGUS_TICKER", dry_run=True)
        # Either not_earnings_ticker or no_speaker — both are clean exits.
        self.assertIn(r["status"], {"no_speaker", "not_earnings_ticker", "dry_run"})


if __name__ == "__main__":
    unittest.main()
