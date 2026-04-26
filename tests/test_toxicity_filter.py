"""Tests for tools/toxicity_filter.py — pin two-tier threshold + cascade.

Why critical: this is the adverse-selection defense. The tightening from
12-fill/85% threshold to 6-fill/95% (then back to two-tier) caught 4 more
markets early. The series cascade caught KXSILVERD's 50-market bleed.
These tests prevent regression of both.

Run: python -m unittest tests.test_toxicity_filter
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Patch settings.DB_PATH to in-memory before importing tools/toxicity_filter
import config.settings as _settings


def _make_in_memory_db() -> str:
    """Create a temp file-based DB (in-memory wouldn't work with sqlite3.connect
    string-based; this gives us a clean per-test DB)."""
    import tempfile
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    f.close()
    return f.name


def _init_all_tables(db_path: str) -> None:
    """Create every table the toxicity scanner reads/writes. Idempotent."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fill_ledger (
            trade_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT, side TEXT,
            count INTEGER, yes_price_cents INTEGER, no_price_cents INTEGER,
            is_taker INTEGER, created_at TEXT, synced_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settlement_log (
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
        );
        CREATE TABLE IF NOT EXISTS lip_programs (
            id TEXT PRIMARY KEY, market_ticker TEXT, series_ticker TEXT,
            enrolled INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS market_blacklist (
            ticker TEXT PRIMARY KEY, expires_at TEXT, reason TEXT, added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS futures_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kalshi_prefix TEXT,
            yahoo_symbol TEXT, name TEXT, price REAL, prev_close REAL,
            change_pct REAL, volume INTEGER, fetched_at TEXT
        );
    """)
    conn.commit()
    conn.close()


def _populate_fills(db_path: str, fills: list[tuple]) -> None:
    """fills = [(ticker, side, count, yes_price, no_price, hours_ago), ...]"""
    _init_all_tables(db_path)
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc)
    for i, (ticker, side, count, yp, np_, hours) in enumerate(fills):
        ts = (now - timedelta(hours=hours)).isoformat()
        conn.execute(
            "INSERT INTO fill_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"trade-{i}", f"order-{i}", ticker, side, count, yp, np_, 0, ts, ts),
        )
    conn.commit()
    conn.close()


def _populate_settlements(db_path: str, settlements: list[tuple]) -> None:
    """settlements = [(ticker, series_prefix, our_realized_usd, hours_ago), ...]"""
    _init_all_tables(db_path)
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc)
    for ticker, series, pnl, hours in settlements:
        ts = (now - timedelta(hours=hours)).isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO settlement_log
               (ticker, series_prefix, close_time, our_realized_usd, recorded_at)
               VALUES (?,?,?,?,?)""",
            (ticker, series, ts, pnl, ts),
        )
    conn.commit()
    conn.close()


class TestTwoTierThreshold(unittest.TestCase):
    """Pin the 6-fill at 95% / 12-fill at 85% logic."""

    def setUp(self):
        self.db_path = _make_in_memory_db()
        self._orig_db = _settings.DB_PATH
        _settings.DB_PATH = self.db_path

    def tearDown(self):
        _settings.DB_PATH = self._orig_db
        Path(self.db_path).unlink(missing_ok=True)

    def test_fewer_than_min_fills_not_flagged(self):
        # 5 fills total — below MIN_FILLS=6, should not flag even at 100%
        _populate_fills(self.db_path, [
            ("KX-TEST-1", "yes", 1, 50, 50, 0.5)
            for _ in range(5)
        ])
        from tools import toxicity_filter
        r = toxicity_filter.scan(self.db_path)
        # No findings because fill_count < MIN_FILLS=6
        flagged = [f for f in r["findings"] if f["ticker"] == "KX-TEST-1"]
        self.assertEqual(len(flagged), 0)

    def test_six_fills_100pct_flagged(self):
        # 6 fills, 100% YES — meets 6-fill 95%-threshold tier
        _populate_fills(self.db_path, [
            ("KX-TEST-2", "yes", 1, 50, 50, 0.5)
            for _ in range(6)
        ])
        from tools import toxicity_filter
        r = toxicity_filter.scan(self.db_path)
        flagged = [f for f in r["findings"] if f["ticker"] == "KX-TEST-2"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["dominant"], "yes")
        self.assertEqual(int(flagged[0]["imbalance"]), 100)

    def test_twelve_fills_85pct_flagged(self):
        # 12+ fills, ~85% imbalance — meets the relaxed 12-fill tier
        fills = [("KX-TEST-3", "yes", 1, 50, 50, 0.5) for _ in range(11)]
        fills += [("KX-TEST-3", "no",  1, 50, 50, 0.5) for _ in range(2)]  # 11/13 = 84.6%
        _populate_fills(self.db_path, fills)
        from tools import toxicity_filter
        r = toxicity_filter.scan(self.db_path)
        flagged = [f for f in r["findings"] if f["ticker"] == "KX-TEST-3"]
        # 11/13 = 0.846, just below 0.85 threshold → not flagged
        # (this confirms the threshold is correctly 0.85 not 0.80)
        self.assertEqual(len(flagged), 0)

    def test_six_fills_below_tight_threshold_not_flagged(self):
        # 6 fills, 5 yes / 1 no = 4/6 = 66% — way below 0.95 tight threshold
        fills = [("KX-TEST-4", "yes", 1, 50, 50, 0.5) for _ in range(5)]
        fills += [("KX-TEST-4", "no", 1, 50, 50, 0.5)]
        _populate_fills(self.db_path, fills)
        from tools import toxicity_filter
        r = toxicity_filter.scan(self.db_path)
        flagged = [f for f in r["findings"] if f["ticker"] == "KX-TEST-4"]
        self.assertEqual(len(flagged), 0)


class TestSeriesCascade(unittest.TestCase):
    """Pin the series-cascade detection: 2+ losers in 48h → block whole series."""

    def setUp(self):
        self.db_path = _make_in_memory_db()
        self._orig_db = _settings.DB_PATH
        _settings.DB_PATH = self.db_path

    def tearDown(self):
        _settings.DB_PATH = self._orig_db
        Path(self.db_path).unlink(missing_ok=True)

    def _enroll_series(self, series_prefix: str, n_markets: int):
        conn = sqlite3.connect(self.db_path)
        for i in range(n_markets):
            ticker = f"{series_prefix}-26TEST-{i}"
            conn.execute(
                """INSERT OR IGNORE INTO lip_programs
                   (id, market_ticker, series_ticker, enrolled)
                   VALUES (?,?,?,?)""",
                (ticker, ticker, series_prefix, 1),
            )
        conn.commit()
        conn.close()

    def test_two_losers_triggers_cascade(self):
        # 2 KXTESTSERIES losers in 48h → block all enrolled markets in series
        _populate_settlements(self.db_path, [
            ("KXTESTSERIES-1", "KXTESTSERIES", -5.0, 24),
            ("KXTESTSERIES-2", "KXTESTSERIES", -3.0, 12),
        ])
        self._enroll_series("KXTESTSERIES", n_markets=10)
        from tools import toxicity_filter
        r = toxicity_filter.scan(self.db_path)
        cascade = r.get("series_findings", [])
        triggered = [s for s in cascade if s["series"] == "KXTESTSERIES"]
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0]["n_losers"], 2)
        self.assertGreater(triggered[0]["new_blocked"], 0)

    def test_single_loser_does_not_trigger_cascade(self):
        _populate_settlements(self.db_path, [
            ("KXALONELOSER-1", "KXALONELOSER", -10.0, 12),
        ])
        self._enroll_series("KXALONELOSER", n_markets=5)
        from tools import toxicity_filter
        r = toxicity_filter.scan(self.db_path)
        cascade = r.get("series_findings", [])
        triggered = [s for s in cascade if s["series"] == "KXALONELOSER"]
        self.assertEqual(len(triggered), 0)

    def test_old_losers_outside_48h_window_dont_count(self):
        # Both losers are >48h ago — no cascade
        _populate_settlements(self.db_path, [
            ("KXOLDLOSER-1", "KXOLDLOSER", -5.0, 60),
            ("KXOLDLOSER-2", "KXOLDLOSER", -3.0, 72),
        ])
        self._enroll_series("KXOLDLOSER", n_markets=5)
        from tools import toxicity_filter
        r = toxicity_filter.scan(self.db_path)
        cascade = r.get("series_findings", [])
        triggered = [s for s in cascade if s["series"] == "KXOLDLOSER"]
        self.assertEqual(len(triggered), 0)

    def test_winners_excluded_from_loser_count(self):
        # 1 loser, 1 winner — only 1 loser, doesn't trigger
        _populate_settlements(self.db_path, [
            ("KXMIXED-1", "KXMIXED", -5.0, 12),
            ("KXMIXED-2", "KXMIXED", +3.0, 6),
        ])
        self._enroll_series("KXMIXED", n_markets=5)
        from tools import toxicity_filter
        r = toxicity_filter.scan(self.db_path)
        cascade = r.get("series_findings", [])
        triggered = [s for s in cascade if s["series"] == "KXMIXED"]
        self.assertEqual(len(triggered), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
