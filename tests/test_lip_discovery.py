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

from config import settings
from engine.lip_discovery import (
    INCENTIVE_STATUSES, _decide_enrol, _parse_program, discover,
    is_active_clause, is_repeating_series, top_n_to_quote,
)


def _iso(days: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _program(**kw) -> dict:
    """Helper: program dict with sensible defaults (live 7-day window,
    short enough to clear the event-binary days-to-settle gate)."""
    defaults = dict(
        series_ticker="KXTEST",
        reward_per_day_usd=50.0,
        target_size=500,
        discount_factor=0.5,
        paid_out=0,
        start_date=_iso(-1),
        end_date=_iso(+7),
    )
    defaults.update(kw)
    return defaults


class TestBlocklistPrefixMatch(unittest.TestCase):
    """Pin the prefix-match blocklist behavior (post 2026-04-22 fix).

    2026-05-05 SURGICAL: family bans (KXNFL/KXNBA/KXMLB) were replaced with
    specific SIG-dominated subseries (KXNBAMVP, KXNFLSB, ...). Prefix
    semantics still apply to those entries; the families themselves are
    now allowed. These tests pin BOTH facts so a regression either way
    (family ban creeping back, or prefix match silently becoming exact)
    is caught.
    """

    def test_exact_match_blocks(self):
        enrol, reason = _decide_enrol(_program(series_ticker="KXNBAMVP"))
        self.assertEqual(enrol, 0)
        self.assertIn("blocklist:series", reason)

    def test_prefix_match_blocks_subseries(self):
        # KXNBAMVP2026 starts with blocklisted KXNBAMVP → must be blocked
        enrol, reason = _decide_enrol(_program(series_ticker="KXNBAMVP2026"))
        self.assertEqual(enrol, 0)
        self.assertIn("KXNBAMVP2026", reason)

    def test_prefix_match_blocks_nfl_subseries(self):
        for sub in ["KXNFLSB", "KXNFLSBWINNER", "KXNFLMVP", "KXNFLMVPODDS"]:
            enrol, _ = _decide_enrol(_program(series_ticker=sub))
            self.assertEqual(enrol, 0, f"{sub} should be blocked")

    def test_prefix_match_blocks_mlb_subseries(self):
        for sub in ["KXMLBWS", "KXMLBWSGAME", "KXMLBMVP", "KXMLBCYY"]:
            enrol, _ = _decide_enrol(_program(series_ticker=sub))
            self.assertEqual(enrol, 0, f"{sub} should be blocked")

    def test_family_level_series_now_allowed(self):
        # The 2026-05-05 policy: prop pools under the family are legitimate.
        for fam in ["KXNBAMENTION", "KXNFLDRAFT", "KXMLBMANAGEROUT"]:
            enrol, reason = _decide_enrol(_program(series_ticker=fam))
            self.assertEqual(enrol, 1, f"{fam} unexpectedly blocked: {reason}")

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
        floor = settings.MIN_REWARD_PER_DAY_USD
        enrol, reason = _decide_enrol(_program(reward_per_day_usd=floor - 0.01))
        self.assertEqual(enrol, 0)
        self.assertIn("reward_too_small", reason)

    def test_reward_at_floor_passes(self):
        floor = settings.MIN_REWARD_PER_DAY_USD
        enrol, reason = _decide_enrol(_program(reward_per_day_usd=floor))
        self.assertEqual(enrol, 1)


class TestTargetSizeCap(unittest.TestCase):
    def test_target_above_cap_blocks(self):
        enrol, reason = _decide_enrol(_program(target_size=20000))
        self.assertEqual(enrol, 0)
        self.assertIn("target_too_large", reason)

    def test_target_at_cap_passes(self):
        enrol, _ = _decide_enrol(_program(target_size=settings.MAX_TARGET_SIZE_CONTRACTS))
        self.assertEqual(enrol, 1)


class TestDiscountFactorFloor(unittest.TestCase):
    def test_discount_below_floor_blocks(self):
        enrol, reason = _decide_enrol(_program(discount_factor=settings.MIN_DISCOUNT_FACTOR - 0.01))
        self.assertEqual(enrol, 0)
        self.assertIn("discount_too_low", reason)

    def test_discount_at_floor_passes(self):
        enrol, _ = _decide_enrol(_program(discount_factor=settings.MIN_DISCOUNT_FACTOR))
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
        # Blocklisted market with high reward — blocklist should still block
        enrol, reason = _decide_enrol(_program(
            series_ticker="KXNBAMVP",
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
            -- period_seconds intentionally omitted: _ensure_schema must add it
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
                start_date="2026-04-01", target_size=500, paid_out=0, enrolled=1):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO lip_programs
               (id, market_ticker, series_ticker, start_date, end_date,
                period_reward_usd, discount_factor, target_size, paid_out,
                enrolled, blocked_reason, reward_per_day_usd, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, 0.5, ?, ?, ?, NULL, ?, '2026-04-28')""",
            (market, market, series, start_date, end_date, reward, target_size,
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
        corn_pri = settings.SIZE_MULTIPLIER_BY_SERIES["KXCORNW"]
        self._insert("CORN-1", "KXCORNW", 50.0)
        self._insert("RAND-1", "KXNOPRI", 50.0)   # default 1.0
        markets = top_n_to_quote(n=10, db_path=self.db_path)
        by_ticker = {m["market_ticker"]: m for m in markets}
        self.assertEqual(by_ticker["CORN-1"]["series_priority"], corn_pri)
        self.assertEqual(by_ticker["RAND-1"]["series_priority"], 1.0)
        # realized_mult=0.7 for unproven series; comp_mult=1.0 (no density data)
        self.assertAlmostEqual(by_ticker["CORN-1"]["priority_weighted_reward"],
                               50.0 * corn_pri * 0.7)
        self.assertAlmostEqual(by_ticker["RAND-1"]["priority_weighted_reward"],
                               50.0 * 0.7)

    def test_not_yet_started_markets_excluded(self):
        """Audit #2: an upcoming program (start_date in the future) is
        persisted but must NOT be quotable until its window opens."""
        self._insert("FUTURE-1", "KXFOO", 200.0, start_date="2098-01-01")
        self._insert("LIVE-1",   "KXFOO",  10.0)
        markets = top_n_to_quote(n=10, db_path=self.db_path)
        tickers = {m["market_ticker"] for m in markets}
        self.assertIn("LIVE-1", tickers)
        self.assertNotIn("FUTURE-1", tickers)

    def test_rows_carry_pool_and_period_seconds(self):
        self._insert("LIVE-1", "KXFOO", 10.0)
        m = top_n_to_quote(n=10, db_path=self.db_path)[0]
        self.assertIn("period_reward_usd", m)
        self.assertIn("period_seconds", m)   # NULL on legacy rows; column added by _ensure_schema
        self.assertEqual(m["period_reward_usd"], 10.0)


class TestActiveClause(unittest.TestCase):
    def test_gates_both_ends_of_window(self):
        c = is_active_clause("p")
        self.assertIn("p.enrolled = 1", c)
        self.assertIn("p.paid_out = 0", c)
        self.assertIn("datetime(p.start_date) <= datetime('now')", c)
        self.assertIn("datetime(p.end_date) > datetime('now')", c)


class TestParseProgram(unittest.TestCase):
    """Audit #2: exact window length, total pool preserved, malformed rows rejected."""

    def _raw(self, **kw):
        d = dict(id="p1", market_ticker="KXTEST-1", incentive_type="liquidity",
                 period_reward=1_000_000,          # centi-cents → $100
                 discount_factor_bps=5000, target_size_fp="500.00",
                 start_date="2026-09-01T00:00:00Z", end_date="2026-09-02T12:00:00Z",
                 paid_out=False)
        d.update(kw)
        return d

    def test_exact_duration_not_floored_days(self):
        p = _parse_program(self._raw())            # 36h window
        self.assertEqual(p["period_seconds"], 36 * 3600)
        self.assertEqual(p["period_reward_usd"], 100.0)          # total pool intact
        self.assertAlmostEqual(p["reward_per_day_usd"], 100.0 / 1.5)  # old code: 100/1 (floor to 1 day)

    def test_sub_day_window(self):
        p = _parse_program(self._raw(end_date="2026-09-01T06:00:00Z"))   # 6h
        self.assertEqual(p["period_seconds"], 6 * 3600)
        self.assertAlmostEqual(p["reward_per_day_usd"], 400.0)

    def test_offset_timezones_normalized(self):
        p = _parse_program(self._raw(start_date="2026-09-01T00:00:00+02:00",
                                     end_date="2026-09-01T00:00:00Z"))
        self.assertEqual(p["period_seconds"], 2 * 3600)

    def test_rejects_end_before_or_equal_start(self):
        self.assertIsNone(_parse_program(self._raw(end_date="2026-09-01T00:00:00Z")))
        self.assertIsNone(_parse_program(self._raw(end_date="2026-08-31T00:00:00Z")))

    def test_rejects_missing_or_garbage_dates(self):
        self.assertIsNone(_parse_program(self._raw(start_date=None)))
        self.assertIsNone(_parse_program(self._raw(end_date="not a date")))
        self.assertIsNone(_parse_program(self._raw(end_date="")))

    def test_rejects_nonfinite_or_nonpositive_numbers(self):
        self.assertIsNone(_parse_program(self._raw(period_reward=0)))
        self.assertIsNone(_parse_program(self._raw(period_reward=-5)))
        self.assertIsNone(_parse_program(self._raw(period_reward="nan")))
        self.assertIsNone(_parse_program(self._raw(period_reward="inf")))
        self.assertIsNone(_parse_program(self._raw(period_reward="abc")))
        self.assertIsNone(_parse_program(self._raw(target_size_fp="0")))
        self.assertIsNone(_parse_program(self._raw(target_size_fp=None)))

    def test_rejects_discount_outside_unit_interval(self):
        self.assertIsNone(_parse_program(self._raw(discount_factor_bps=0)))
        self.assertIsNone(_parse_program(self._raw(discount_factor_bps=10_001)))
        self.assertIsNone(_parse_program(self._raw(discount_factor_bps=None)))
        self.assertEqual(_parse_program(self._raw(discount_factor_bps=10_000))["discount_factor"], 1.0)


class TestDecideEnrolWindow(unittest.TestCase):
    def test_expired_blocks(self):
        enrol, reason = _decide_enrol(_program(start_date="2019-01-01T00:00:00Z",
                                               end_date="2020-01-01T00:00:00Z"))
        self.assertEqual((enrol, reason), (0, "expired"))

    def test_expiry_compares_datetimes_not_strings(self):
        # 'Z' vs '+00:00' spelling must not change the verdict
        now = "2026-09-20T12:00:00+00:00"
        # start_date must be pinned too. The helper defaults it to
        # (real now - 1 day), so against a hardcoded end_date this test
        # passed only while the wall clock's time-of-day was earlier than
        # 11:59:59Z, and read as "malformed_window" afterwards. The subject
        # here is Z vs +00:00 spelling, not window ordering.
        enrol, reason = _decide_enrol(
            _program(start_date="2026-09-19T00:00:00Z",
                     end_date="2026-09-20T11:59:59Z"), now_iso=now)
        self.assertEqual(reason, "expired")
        enrol, reason = _decide_enrol(
            _program(start_date="2026-09-19T00:00:00Z",
                     end_date="2026-09-20T12:00:01Z"), now_iso=now)
        self.assertEqual(enrol, 1)

    def test_upcoming_is_enrolled_but_runtime_gated(self):
        # Enrolment is our decision; the start gate lives in is_active_clause.
        enrol, reason = _decide_enrol(_program(start_date=_iso(+1), end_date=_iso(+8)))
        self.assertEqual((enrol, reason), (1, "ok"))

    def test_malformed_window_blocks(self):
        self.assertEqual(_decide_enrol(_program(end_date="garbage"))[1], "malformed_window")
        self.assertEqual(_decide_enrol(_program(start_date=_iso(+8), end_date=_iso(+7)))[1],
                         "malformed_window")

    def test_missing_dates_tolerated(self):
        # Rows from older fixtures / callers without a window still get a verdict.
        p = _program(); p.pop("start_date"); p.pop("end_date")
        self.assertEqual(_decide_enrol(p)[0], 1)


class TestDiscoverStatuses(unittest.TestCase):
    """Audit #2: query the statuses Kalshi actually defines, skip bad rows."""

    class _FakeClient:
        calls: list = []
        rows: list = []

        def get_unauth(self, path, params=None):
            type(self).calls.append(dict(params or {}))
            return {"incentive_programs": list(type(self).rows), "next_cursor": None}

    def setUp(self):
        self._FakeClient.calls = []
        self._FakeClient.rows = []

    def test_statuses_match_api_vocabulary(self):
        self.assertEqual(INCENTIVE_STATUSES, ("active", "upcoming", "closed", "paid_out"))
        with patch("engine.lip_discovery.KalshiClient", self._FakeClient):
            discover(save=False)
        self.assertEqual([c["status"] for c in self._FakeClient.calls], list(INCENTIVE_STATUSES))
        for c in self._FakeClient.calls:
            self.assertEqual(c["type"], "liquidity")

    def test_malformed_row_skipped_not_fatal(self):
        good = dict(id="ok", market_ticker="KXA-1", incentive_type="liquidity",
                    period_reward=100_000, discount_factor_bps=5000, target_size_fp="100",
                    start_date="2026-09-01T00:00:00Z", end_date="2026-09-08T00:00:00Z")
        bad = dict(good, id="bad", market_ticker="KXB-1", end_date="2026-08-01T00:00:00Z")
        self._FakeClient.rows = [bad, good, dict(good, id="vol", incentive_type="volume")]
        with patch("engine.lip_discovery.KalshiClient", self._FakeClient):
            out = discover(status="active", save=False)
        self.assertEqual([p["id"] for p in out], ["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
