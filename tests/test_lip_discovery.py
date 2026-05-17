"""Tests for engine/lip_discovery._decide_enrol — the enrollment filter logic.

Why critical: this gate decides which markets we touch. The KXNBAMENTION
incident (2026-04-22) was caused by exact-match instead of prefix-match
blocklist letting 125 NBA prop markets through ($2.5k/day potential
exposure to SIG-dominated markets). These tests pin the prefix-match fix.

Run: python -m unittest tests.test_lip_discovery
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.lip_discovery import _decide_enrol, top_n_to_quote, is_repeating_series


def _program(**kw) -> dict:
    """Helper: program dict with sensible defaults."""
    defaults = dict(
        series_ticker="KXTEST",
        reward_per_day_usd=50.0,
        target_size=500,
        discount_factor=0.5,
        paid_out=0,
    )
    defaults.update(kw)
    return defaults


class TestBlocklistPrefixMatch(unittest.TestCase):
    """Pin the prefix-match blocklist behavior (post 2026-04-22 fix)."""

    def test_exact_match_blocks(self):
        # Prior to the fix this was the only thing that worked
        enrol, reason = _decide_enrol(_program(series_ticker="KXNFL"))
        self.assertEqual(enrol, 0)
        self.assertIn("blocklist:series", reason)

    def test_prefix_match_blocks_subseries(self):
        # The KEY fix: KXNBAMENTION starts with KXNBA → must be blocked
        enrol, reason = _decide_enrol(_program(series_ticker="KXNBAMENTION"))
        self.assertEqual(enrol, 0)
        self.assertIn("KXNBAMENTION", reason)

    def test_prefix_match_blocks_nfl_subseries(self):
        for sub in ["KXNFLDRAFT", "KXNFLDRAFTCAT", "KXNFLDRAFTOU", "KXNFLDRAFTTOP"]:
            enrol, _ = _decide_enrol(_program(series_ticker=sub))
            self.assertEqual(enrol, 0, f"{sub} should be blocked")

    def test_prefix_match_blocks_mlb_subseries(self):
        for sub in ["KXMLBSERIES", "KXMLBAWARDCOMBO", "KXMLBTRADE"]:
            enrol, _ = _decide_enrol(_program(series_ticker=sub))
            self.assertEqual(enrol, 0, f"{sub} should be blocked")

    def test_non_blocked_series_passes(self):
        enrol, reason = _decide_enrol(_program(series_ticker="KXBRENTD"))
        self.assertEqual(enrol, 1)
        self.assertEqual(reason, "ok")

    def test_empty_series_ticker_does_not_crash(self):
        # Defensive: missing series_ticker should fall through, not crash
        enrol, _ = _decide_enrol(_program(series_ticker=""))
        # With empty string, no startswith matches, so passes through to other gates
        self.assertEqual(enrol, 1)

    def test_none_series_ticker_does_not_crash(self):
        # Old data may have None — explicit handling
        enrol, _ = _decide_enrol(_program(series_ticker=None))
        self.assertEqual(enrol, 1)  # falls through to ok


class TestRewardFloor(unittest.TestCase):
    def test_reward_below_floor_blocks(self):
        enrol, reason = _decide_enrol(_program(reward_per_day_usd=5.0))
        self.assertEqual(enrol, 0)
        self.assertIn("reward_too_small", reason)

    def test_reward_at_floor_passes(self):
        # MIN_REWARD_PER_DAY_USD = 10.0 per settings
        enrol, reason = _decide_enrol(_program(reward_per_day_usd=10.0))
        self.assertEqual(enrol, 1)


class TestTargetSizeCap(unittest.TestCase):
    def test_target_above_cap_blocks(self):
        enrol, reason = _decide_enrol(_program(target_size=20000))
        self.assertEqual(enrol, 0)
        self.assertIn("target_too_large", reason)

    def test_target_at_cap_passes(self):
        # MAX_TARGET_SIZE_CONTRACTS = 19999
        enrol, _ = _decide_enrol(_program(target_size=19999))
        self.assertEqual(enrol, 1)


class TestDiscountFactorFloor(unittest.TestCase):
    def test_discount_below_floor_blocks(self):
        enrol, reason = _decide_enrol(_program(discount_factor=0.4))
        self.assertEqual(enrol, 0)
        self.assertIn("discount_too_low", reason)

    def test_discount_at_floor_passes(self):
        enrol, _ = _decide_enrol(_program(discount_factor=0.5))
        self.assertEqual(enrol, 1)


class TestPaidOutGate(unittest.TestCase):
    def test_paid_out_blocks(self):
        enrol, reason = _decide_enrol(_program(paid_out=1))
        self.assertEqual(enrol, 0)
        self.assertEqual(reason, "already_paid_out")

    def test_not_paid_out_passes(self):
        enrol, _ = _decide_enrol(_program(paid_out=0))
        self.assertEqual(enrol, 1)


class TestGateOrdering(unittest.TestCase):
    """Verify gates fire in expected order — blocklist FIRST so we don't
    waste subsequent checks on excluded series."""

    def test_blocklist_fires_before_reward_check(self):
        # NBA market with high reward — blocklist should still block
        enrol, reason = _decide_enrol(_program(
            series_ticker="KXNBAMENTION",
            reward_per_day_usd=500.0,
        ))
        self.assertEqual(enrol, 0)
        self.assertIn("blocklist", reason)


class TestIsRepeatingSeries(unittest.TestCase):
    """Pin the prefix-only whitelist for recurring series (2026-05-12 v2).

    Why: suffix-only fallback (any *WEEKLY / *MON) used to exempt
    geopolitical and event "weeklies" from the days-to-settle gate. After
    KXHORMUZWEEKLY bled -$15 the rule tightened to prefix-only.
    """

    def test_commodity_prefix_recurs(self):
        self.assertTrue(is_repeating_series("KXCORNW"))
        self.assertTrue(is_repeating_series("KXBRENTD"))
        self.assertTrue(is_repeating_series("KXWHEATMON"))

    def test_weather_prefix_recurs(self):
        self.assertTrue(is_repeating_series("KXHIGHTNY"))
        self.assertTrue(is_repeating_series("KXLOWTMIA"))
        self.assertTrue(is_repeating_series("KXRAINLAX"))

    def test_crypto_prefix_recurs(self):
        self.assertTrue(is_repeating_series("KXBTC15M"))
        self.assertTrue(is_repeating_series("KXETHMAXMON"))

    def test_geopolitical_weekly_blocked(self):
        # Was the BUG — suffix WEEKLY exempted it. Now must be rejected.
        self.assertFalse(is_repeating_series("KXHORMUZWEEKLY"))

    def test_event_weekly_blocked(self):
        # KXEOWEEK = executive orders weekly — bled $48 in 6h before ban.
        self.assertFalse(is_repeating_series("KXEOWEEK"))

    def test_event_binary_blocked(self):
        # No suffix, no whitelisted prefix — definitely event binary.
        self.assertFalse(is_repeating_series("KXJIMMYKIMMELFIRED"))
        self.assertFalse(is_repeating_series("KXMAKARYOUT"))

    def test_empty_input_blocked(self):
        self.assertFalse(is_repeating_series(""))
        self.assertFalse(is_repeating_series(None))


class TestTopNPriorityWeighting(unittest.TestCase):
    """Pin the #100 priority-weighted ranking behavior (2026-04-28).

    Verifies that:
      1. Series in SIZE_MULTIPLIER_BY_SERIES get reward × priority lift
      2. Default series (priority=1.0) ordered by raw reward
      3. Higher priority can outrank a higher raw reward
      4. End-date filter excludes settled markets
    """

    def setUp(self):
        import tempfile, sqlite3
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_file.close()
        self.db_path = self.db_file.name
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE lip_programs (
                id TEXT PRIMARY KEY, market_ticker TEXT, series_ticker TEXT,
                start_date TEXT, end_date TEXT, period_reward_usd REAL,
                discount_factor REAL, target_size REAL, paid_out INTEGER,
                enrolled INTEGER, blocked_reason TEXT,
                reward_per_day_usd REAL, last_seen TEXT
            );
            CREATE TABLE market_blacklist (
                ticker TEXT PRIMARY KEY, expires_at TEXT, reason TEXT, added_at TEXT
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        import os
        os.unlink(self.db_path)

    def _insert(self, market, series, reward, *, end_date="2099-12-31",
                target_size=500, paid_out=0, enrolled=1):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO lip_programs VALUES
               (?, ?, ?, '2026-04-01', ?, ?, 0.5, ?, ?, ?, NULL, ?, '2026-04-28')""",
            (market, market, series, end_date, reward, target_size,
             paid_out, enrolled, reward),
        )
        conn.commit()
        conn.close()

    def test_higher_priority_outranks_higher_reward(self):
        # KXBRENTD is a 2.0x winner per SIZE_MULTIPLIER_BY_SERIES
        self._insert("KXBOGUS-1", "KXBOGUS", 100.0)   # priority 1.0 → wtd 100
        self._insert("KXBRENTD-1", "KXBRENTD", 60.0)  # priority 2.0 → wtd 120
        markets = top_n_to_quote(n=2, db_path=self.db_path)
        self.assertEqual(markets[0]["market_ticker"], "KXBRENTD-1")
        self.assertEqual(markets[1]["market_ticker"], "KXBOGUS-1")

    def test_default_priority_orders_by_raw_reward(self):
        self._insert("A-1", "KXAAA", 50.0)
        self._insert("B-1", "KXBBB", 100.0)
        markets = top_n_to_quote(n=2, db_path=self.db_path)
        self.assertEqual(markets[0]["market_ticker"], "B-1")
        self.assertEqual(markets[1]["market_ticker"], "A-1")

    def test_settled_markets_excluded(self):
        # end_date in the past → must not appear
        self._insert("STALE-1", "KXFOO", 200.0, end_date="2020-01-01")
        self._insert("LIVE-1",  "KXFOO",  10.0)  # still live
        markets = top_n_to_quote(n=10, db_path=self.db_path)
        tickers = {m["market_ticker"] for m in markets}
        self.assertIn("LIVE-1", tickers)
        self.assertNotIn("STALE-1", tickers)

    def test_blacklisted_markets_excluded(self):
        import sqlite3
        self._insert("BLOCKED-1", "KXFOO", 200.0)
        self._insert("FINE-1", "KXFOO", 100.0)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO market_blacklist VALUES
               (?, '2099-12-31', 'test', '2026-04-28')""",
            ("BLOCKED-1",),
        )
        conn.commit()
        conn.close()
        markets = top_n_to_quote(n=10, db_path=self.db_path)
        tickers = {m["market_ticker"] for m in markets}
        self.assertNotIn("BLOCKED-1", tickers)
        self.assertIn("FINE-1", tickers)

    def test_priority_field_present_and_correct(self):
        self._insert("CORN-1", "KXCORNW", 50.0)   # 2.0x in table
        self._insert("RAND-1", "KXNOPRI", 50.0)   # default 1.0
        markets = top_n_to_quote(n=10, db_path=self.db_path)
        by_ticker = {m["market_ticker"]: m for m in markets}
        self.assertEqual(by_ticker["CORN-1"]["series_priority"], 2.0)
        self.assertEqual(by_ticker["RAND-1"]["series_priority"], 1.0)
        self.assertAlmostEqual(by_ticker["CORN-1"]["priority_weighted_reward"], 100.0)
        self.assertAlmostEqual(by_ticker["RAND-1"]["priority_weighted_reward"],  50.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
