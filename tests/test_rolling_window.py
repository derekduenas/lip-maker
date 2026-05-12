"""Tests for engine/rolling_window — Poisson math + hard-gate behavior."""
from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestPoissonMath(unittest.TestCase):
    def test_cdf_at_zero(self):
        from engine.rolling_window import _poisson_cdf
        # P(X ≤ 0 | λ=1) = e^{-1} ≈ 0.368
        self.assertAlmostEqual(_poisson_cdf(0, 1.0), math.exp(-1.0), places=3)

    def test_at_least_n_clamped(self):
        from engine.rolling_window import p_at_least_n
        # Way above expected → near 0
        self.assertLess(p_at_least_n(100, 2.0), 0.0001)
        # Way below expected → near 1
        self.assertGreater(p_at_least_n(1, 50.0), 0.9999)
        # At expected → roughly 0.5
        self.assertGreater(p_at_least_n(5, 5.0), 0.4)
        self.assertLess(p_at_least_n(5, 5.0), 0.7)

    def test_n_zero_always_one(self):
        from engine.rolling_window import p_at_least_n
        self.assertEqual(p_at_least_n(0, 0.0), 1.0)
        self.assertEqual(p_at_least_n(0, 10.0), 1.0)


class _TempDBMixin:
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db_path = self.tmp.name
        self._patchers = [
            patch("config.settings.DB_PATH", self.db_path),
            patch("engine.rolling_window.DB_PATH", self.db_path),
        ]
        for p in self._patchers:
            p.start()
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, source TEXT, speaker TEXT,
            event_type TEXT, event_date DATE, raw_text TEXT, word_count INTEGER
        );
        CREATE TABLE mentions (
            id INTEGER PRIMARY KEY, transcript_id INTEGER,
            term TEXT, mentioned BOOLEAN, mention_count INTEGER,
            context_snippet TEXT, UNIQUE(transcript_id, term)
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

    def _insert_speeches(self, n: int, term: str, count_per_speech: int,
                        speaker: str = "trump", event_type: str = "trump_speech"):
        """Insert n transcripts spaced one month apart, each mentioning the
        term `count_per_speech` times."""
        conn = sqlite3.connect(self.db_path)
        for i in range(n):
            d = dt.date(2025, 1, 1) + dt.timedelta(days=30 * i)
            cur = conn.execute(
                """INSERT INTO transcripts (source, speaker, event_type, event_date,
                                            raw_text, word_count)
                   VALUES ('test', ?, ?, ?, '...', 1000)""",
                (speaker, event_type, d.isoformat()),
            )
            tid = cur.lastrowid
            if count_per_speech > 0:
                conn.execute(
                    """INSERT INTO mentions (transcript_id, term, mentioned,
                                              mention_count)
                       VALUES (?, ?, 1, ?)""",
                    (tid, term, count_per_speech),
                )
        conn.commit()
        conn.close()


class TestHardGates(_TempDBMixin, unittest.TestCase):
    def test_skip_no_corpus(self):
        from engine.rolling_window import predict_rolling
        conn = sqlite3.connect(self.db_path)
        pred = predict_rolling(
            conn, speaker="trump", event_type="trump_speech",
            term="tariffs", threshold_n=5,
            settle_date=dt.date(2026, 6, 1),
            today=dt.date(2026, 5, 15),
        )
        self.assertEqual(pred.status, "skip_no_corpus")
        self.assertIsNone(pred.p_yes)

    def test_skip_thin_cadence(self):
        # 5 speeches < MIN_CADENCE_OBS (8) → SKIP
        self._insert_speeches(n=5, term="tariffs", count_per_speech=2)
        from engine.rolling_window import predict_rolling
        conn = sqlite3.connect(self.db_path)
        pred = predict_rolling(
            conn, speaker="trump", event_type="trump_speech",
            term="tariffs", threshold_n=5,
            settle_date=dt.date(2026, 6, 1),
            today=dt.date(2026, 5, 15),
        )
        self.assertEqual(pred.status, "skip_thin_cadence")

    def test_skip_thin_term(self):
        # 10 speeches but only 1 mentions the term → SKIP
        self._insert_speeches(n=9, term="other", count_per_speech=1)
        self._insert_speeches(n=1, term="rare_term", count_per_speech=1)
        from engine.rolling_window import predict_rolling
        conn = sqlite3.connect(self.db_path)
        pred = predict_rolling(
            conn, speaker="trump", event_type="trump_speech",
            term="rare_term", threshold_n=5,
            settle_date=dt.date(2026, 6, 1),
            today=dt.date(2026, 5, 15),
        )
        self.assertEqual(pred.status, "skip_thin_term")

    def test_passes_with_sufficient_data(self):
        # 12 speeches, each mentioning term 3 times → passes both gates
        self._insert_speeches(n=12, term="tariffs", count_per_speech=3)
        from engine.rolling_window import predict_rolling
        conn = sqlite3.connect(self.db_path)
        pred = predict_rolling(
            conn, speaker="trump", event_type="trump_speech",
            term="tariffs", threshold_n=10,
            settle_date=dt.date(2026, 6, 1),
            today=dt.date(2026, 5, 15),
        )
        self.assertEqual(pred.status, "ok")
        self.assertIsNotNone(pred.p_yes)
        # cadence ≈ 1/month (one speech every 30 days), 16 days remaining
        # in May → expected_speeches_remaining ≈ 0.53
        # per_speech_rate = 3, expected_total ≈ 1.6
        # P(≥10) when expected=1.6 → very low
        self.assertLess(pred.p_yes, 0.05)


class TestDecompositionSensibility(_TempDBMixin, unittest.TestCase):
    """The user's smell-test: heavy mentions + plenty of days → near 1.
    Few days + thin term → near 0. Close call → near 0.5."""

    def test_heavy_overshoot_returns_near_one(self):
        # 12 speeches each mentioning term 10 times → λ_per = 10
        self._insert_speeches(n=12, term="tariffs", count_per_speech=10)
        from engine.rolling_window import predict_rolling
        conn = sqlite3.connect(self.db_path)
        # 30 days remaining, cadence 1/mo, expected_total ≈ 1×10 = 10
        # threshold 3 → P(≥3) high
        pred = predict_rolling(
            conn, speaker="trump", event_type="trump_speech",
            term="tariffs", threshold_n=3,
            settle_date=dt.date(2026, 5, 31),
            today=dt.date(2026, 5, 1),
        )
        self.assertEqual(pred.status, "ok")
        self.assertGreater(pred.p_yes, 0.95)

    def test_undershoot_returns_near_zero(self):
        # 12 speeches each mentioning term once → λ_per = 1
        self._insert_speeches(n=12, term="tariffs", count_per_speech=1)
        from engine.rolling_window import predict_rolling
        conn = sqlite3.connect(self.db_path)
        # 3 days remaining, cadence 1/mo → expected_speeches ≈ 0.1
        # → expected_total ≈ 0.1
        # threshold 5 → P(≥5) very low
        pred = predict_rolling(
            conn, speaker="trump", event_type="trump_speech",
            term="tariffs", threshold_n=5,
            settle_date=dt.date(2026, 5, 31),
            today=dt.date(2026, 5, 28),
        )
        self.assertEqual(pred.status, "ok")
        self.assertLess(pred.p_yes, 0.05)


if __name__ == "__main__":
    unittest.main()
