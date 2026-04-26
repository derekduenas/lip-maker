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

from engine.lip_discovery import _decide_enrol


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
