"""Tests for execution/quote_manager.py pure helpers — _series_gross,
_total_gross_exposure, _refresh_inventory.

Why critical: these feed the safety gates. Wrong gross calc → wrong cap
enforcement → real money risk. The Skeptic-audit per-series cap fix
(2026-04-22) added _series_gross and these pin its math.

Skips _passes_safety / _cold_boot_reconcile / _place_order — those need
live KalshiClient mocking and are integration-style. Pure helpers only.

Run: python -m unittest tests.test_quote_manager_helpers
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.quote_manager import InventoryState, QuoteManager, RestingOrder


def _fresh_qm() -> QuoteManager:
    """Build a paper-mode QM (skips Kalshi auth + cold-boot)."""
    return QuoteManager(paper=True)


class TestSeriesGross(unittest.TestCase):
    """The Brent flash-crash protection cap. Sums gross USD per series."""

    def test_empty_resting_returns_zero(self):
        qm = _fresh_qm()
        self.assertEqual(qm._series_gross("KXBRENTD"), 0.0)

    def test_single_series_two_orders(self):
        # 2 Brent strikes, each yes+no resting. Gross = sum(price × size / 100)
        qm = _fresh_qm()
        qm.resting["KXBRENTD-26-T100"] = [
            RestingOrder("o1", "KXBRENTD-26-T100", "yes", 50, 10, time.time()),
            RestingOrder("o2", "KXBRENTD-26-T100", "no",  50, 10, time.time()),
        ]
        qm.resting["KXBRENTD-26-T101"] = [
            RestingOrder("o3", "KXBRENTD-26-T101", "yes", 60, 5, time.time()),
        ]
        # Order 1: 50 × 10 / 100 = 5
        # Order 2: 50 × 10 / 100 = 5
        # Order 3: 60 × 5  / 100 = 3
        # Total: 13
        self.assertEqual(qm._series_gross("KXBRENTD"), 13.0)

    def test_other_series_excluded(self):
        qm = _fresh_qm()
        qm.resting["KXBRENTD-26-T100"] = [
            RestingOrder("o1", "KXBRENTD-26-T100", "yes", 50, 10, time.time()),
        ]
        qm.resting["KXGOLDD-26-T2000"] = [
            RestingOrder("o2", "KXGOLDD-26-T2000", "yes", 50, 10, time.time()),
        ]
        # KXBRENTD only counts orders starting with "KXBRENTD-"
        self.assertEqual(qm._series_gross("KXBRENTD"), 5.0)
        self.assertEqual(qm._series_gross("KXGOLDD"), 5.0)

    def test_prefix_match_strict_avoids_partial_match(self):
        # KXBRENT should NOT match KXBRENTW (separate series — Brent weekly)
        qm = _fresh_qm()
        qm.resting["KXBRENTW-26-T100"] = [
            RestingOrder("o1", "KXBRENTW-26-T100", "yes", 50, 10, time.time()),
        ]
        # Searching for KXBRENT (no D/W) — uses "KXBRENT-" so won't match KXBRENTW-
        self.assertEqual(qm._series_gross("KXBRENT"), 0.0)
        # Searching for KXBRENTW does match
        self.assertEqual(qm._series_gross("KXBRENTW"), 5.0)


class TestTotalGrossExposure(unittest.TestCase):
    def test_empty_returns_zero(self):
        qm = _fresh_qm()
        self.assertEqual(qm._total_gross_exposure(), 0.0)

    def test_sums_all_resting_orders_across_markets(self):
        qm = _fresh_qm()
        qm.resting["KX-A"] = [
            RestingOrder("o1", "KX-A", "yes", 30, 10, time.time()),
            RestingOrder("o2", "KX-A", "no",  70, 10, time.time()),
        ]
        qm.resting["KX-B"] = [
            RestingOrder("o3", "KX-B", "yes", 40, 5, time.time()),
        ]
        # 3 + 7 + 2 = 12
        self.assertEqual(qm._total_gross_exposure(), 12.0)


class TestRefreshInventory(unittest.TestCase):
    """The Spread Master fix (2026-04-22): populate self.inventory from
    fill_ledger so MAX_NET_INVENTORY_USD cap actually enforces."""

    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.db = f.name
        # Set up minimal schema
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE fill_ledger (
                trade_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT,
                side TEXT, count INTEGER,
                yes_price_cents INTEGER, no_price_cents INTEGER,
                is_taker INTEGER, created_at TEXT, synced_at TEXT
            );
            CREATE TABLE settlement_log (
                id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, series_prefix TEXT,
                close_time TEXT, our_realized_usd REAL, recorded_at TEXT
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def _add_fill(self, ticker: str, side: str, count: int, price: int):
        conn = sqlite3.connect(self.db)
        now = datetime.now(timezone.utc).isoformat()
        # Use unique trade_id per call (timestamp-based)
        tid = f"trade-{ticker}-{side}-{count}-{price}-{time.time_ns()}"
        yp = price if side == "yes" else 100 - price
        np_ = price if side == "no"  else 100 - price
        conn.execute(
            "INSERT INTO fill_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, "ord", ticker, side, count, yp, np_, 0, now, now),
        )
        conn.commit()
        conn.close()

    def test_no_fills_returns_zero_inventory(self):
        qm = QuoteManager(paper=True, db_path=self.db)
        qm._refresh_inventory("KX-NONE")
        inv = qm.inventory.get("KX-NONE")
        self.assertIsNotNone(inv)
        self.assertEqual(inv.net_yes_contracts, 0)

    def test_only_yes_fills_positive_net(self):
        self._add_fill("KX-LONG-YES", "yes", 5, 30)
        self._add_fill("KX-LONG-YES", "yes", 3, 30)
        qm = QuoteManager(paper=True, db_path=self.db)
        qm._refresh_inventory("KX-LONG-YES")
        self.assertEqual(qm.inventory["KX-LONG-YES"].net_yes_contracts, 8)

    def test_only_no_fills_negative_net(self):
        # Buying NO = shorting YES, so net_yes is negative
        self._add_fill("KX-LONG-NO", "no", 5, 30)
        qm = QuoteManager(paper=True, db_path=self.db)
        qm._refresh_inventory("KX-LONG-NO")
        self.assertEqual(qm.inventory["KX-LONG-NO"].net_yes_contracts, -5)

    def test_mixed_fills_net_correctly(self):
        self._add_fill("KX-MIX", "yes", 10, 30)
        self._add_fill("KX-MIX", "no",  3, 70)
        qm = QuoteManager(paper=True, db_path=self.db)
        qm._refresh_inventory("KX-MIX")
        self.assertEqual(qm.inventory["KX-MIX"].net_yes_contracts, 7)  # 10 - 3

    def test_settled_ticker_excluded(self):
        # Add fills + settlement → inventory should ignore the ticker
        self._add_fill("KX-SETTLED", "yes", 10, 50)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO settlement_log (ticker, series_prefix, close_time, our_realized_usd, recorded_at) VALUES (?,?,?,?,?)",
            ("KX-SETTLED", "KX", "2026-04-25", 5.0, "2026-04-25"),
        )
        conn.commit()
        conn.close()
        qm = QuoteManager(paper=True, db_path=self.db)
        qm._refresh_inventory("KX-SETTLED")
        self.assertEqual(qm.inventory["KX-SETTLED"].net_yes_contracts, 0)

    def test_cache_30s_skips_redundant_query(self):
        # First call queries DB; second call within 30s should use cache
        self._add_fill("KX-CACHE", "yes", 5, 50)
        qm = QuoteManager(paper=True, db_path=self.db)
        qm._refresh_inventory("KX-CACHE")
        first_ts = qm.inventory["KX-CACHE"].last_updated
        # Add a fill — if cache works, second refresh won't see it
        self._add_fill("KX-CACHE", "yes", 5, 50)
        qm._refresh_inventory("KX-CACHE")  # should be cached
        # Same timestamp = wasn't refreshed
        self.assertEqual(qm.inventory["KX-CACHE"].last_updated, first_ts)
        # net unchanged from cached value
        self.assertEqual(qm.inventory["KX-CACHE"].net_yes_contracts, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
