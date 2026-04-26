"""Tests for tools/settlement_dress_rehearsal.py — pin interpretation logic.

Why critical: this script informs the pre-Tuesday read on weekly payout.
Wrong interpretation = miscalibrated expectation = potentially wrong
injection decision Tuesday. Pin every band.

Run: python -m unittest tests.test_settlement_dress_rehearsal
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.settlement_dress_rehearsal import (
    DayReport,
    fetch_day,
    interpret_for_weekly,
)


def _fresh_db_with_settlements(rows: list[tuple]) -> str:
    """rows = [(ticker, series, close_date, realized, rebate, yes_qty, no_qty), ...]"""
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("""
        CREATE TABLE settlement_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, series_prefix TEXT NOT NULL,
            close_time TEXT NOT NULL, strike REAL,
            kalshi_settle_value REAL, kalshi_result TEXT,
            futures_fair REAL, futures_confidence TEXT,
            predicted_result TEXT, prediction_correct INTEGER,
            delta_kalshi_futures REAL, our_position_yes INTEGER,
            our_position_no INTEGER, our_realized_usd REAL,
            rebate_earned_usd REAL, net_outcome_usd REAL, recorded_at TEXT NOT NULL,
            UNIQUE(ticker)
        )
    """)
    for ticker, series, close_date, realized, rebate, yes_q, no_q in rows:
        conn.execute(
            """INSERT INTO settlement_log
               (ticker, series_prefix, close_time, our_position_yes, our_position_no,
                our_realized_usd, rebate_earned_usd, recorded_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticker, series, f"{close_date}T21:00:00Z", yes_q, no_q,
             realized, rebate, "2026-04-26T22:00:00Z"),
        )
    conn.commit()
    conn.close()
    return f.name


class TestFetchDay(unittest.TestCase):
    def test_no_data_returns_empty_report(self):
        db = _fresh_db_with_settlements([])
        r = fetch_day("2026-04-27", db)
        self.assertEqual(r.n_settled, 0)
        self.assertEqual(r.realized_total, 0.0)
        Path(db).unlink()

    def test_single_winner(self):
        db = _fresh_db_with_settlements([
            ("KX-A", "KXBRENTD", "2026-04-27", 5.0, 0.0, 10, 0),
        ])
        r = fetch_day("2026-04-27", db)
        self.assertEqual(r.n_settled, 1)
        self.assertEqual(r.realized_total, 5.0)
        self.assertEqual(r.n_winners, 1)
        self.assertEqual(r.n_losers, 0)
        Path(db).unlink()

    def test_excludes_other_days(self):
        db = _fresh_db_with_settlements([
            ("KX-A", "KXBRENTD", "2026-04-27",  5.0, 0.0, 10, 0),
            ("KX-B", "KXBRENTD", "2026-04-26", -3.0, 0.0,  0, 5),
        ])
        r = fetch_day("2026-04-27", db)
        self.assertEqual(r.n_settled, 1)
        self.assertEqual(r.realized_total, 5.0)
        Path(db).unlink()

    def test_excludes_zero_position(self):
        # Markets with no position shouldn't count even if settled
        db = _fresh_db_with_settlements([
            ("KX-A", "KXBRENTD", "2026-04-27",  5.0, 0.0, 10, 0),
            ("KX-Z", "KXBRENTD", "2026-04-27",  0.0, 0.0,  0, 0),  # no position
        ])
        r = fetch_day("2026-04-27", db)
        self.assertEqual(r.n_settled, 1)
        Path(db).unlink()

    def test_series_breakdown_sorted_worst_first(self):
        db = _fresh_db_with_settlements([
            ("KX-COFFEE-1", "KXCOFFEEW", "2026-04-27", -20.0, 0.0, 0, 30),
            ("KX-BRENT-1",  "KXBRENTD",  "2026-04-27",   5.0, 0.0, 10, 0),
            ("KX-CORN-1",   "KXCORNW",   "2026-04-27",  -3.0, 0.0, 0, 10),
        ])
        r = fetch_day("2026-04-27", db)
        # Worst first: KXCOFFEEW (-20), KXCORNW (-3), KXBRENTD (+5)
        self.assertEqual(r.by_series[0][0], "KXCOFFEEW")
        self.assertEqual(r.by_series[-1][0], "KXBRENTD")
        Path(db).unlink()


class TestInterpretation(unittest.TestCase):
    """Pin the bands that drive Tuesday's expectation."""

    def test_minus_90_predicts_low_band(self):
        msg = interpret_for_weekly(-90.0)
        self.assertIn("LOW BAND", msg)

    def test_minus_60_predicts_low_band(self):
        msg = interpret_for_weekly(-60.0)
        self.assertIn("LOW BAND", msg)

    def test_minus_30_predicts_low_mid(self):
        msg = interpret_for_weekly(-30.0)
        self.assertIn("LOW-MID", msg)

    def test_zero_predicts_mid(self):
        msg = interpret_for_weekly(0.0)
        self.assertIn("MID", msg)
        self.assertNotIn("LOW", msg)

    def test_plus_5_predicts_mid(self):
        msg = interpret_for_weekly(5.0)
        self.assertIn("MID", msg)

    def test_plus_50_predicts_high(self):
        msg = interpret_for_weekly(50.0)
        self.assertIn("HIGH", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
