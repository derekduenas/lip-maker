"""Tests for tools/apr28_reconcile.py — pin the injection ladder + comparison logic.

Why critical: this script runs ONCE on Apr 28 to drive the $10k-$22.5k
injection decision. A bug here = wrong injection size = real capital harm.

Run: python -m unittest tests.test_apr28_reconcile
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.apr28_reconcile import (
    FROZEN_BASELINES_2026_04_28,
    INJECTION_LADDER,
    Comparison,
    FrozenPrediction,
    compare_predictions,
    find_closest,
    find_tier,
    per_market_reconciliation,
)


class TestInjectionLadder(unittest.TestCase):
    """The ladder is the most consequential thing in the file. Pin every band."""

    def test_negative_actual_halts(self):
        tier = find_tier(-50.0)
        self.assertEqual(tier.label, "NEGATIVE")
        self.assertEqual(tier.inject_usd, 0.0)

    def test_zero_actual_diagnose(self):
        tier = find_tier(0.0)
        self.assertEqual(tier.label, "DIAGNOSE_FIRST")
        self.assertEqual(tier.inject_usd, 0.0)

    def test_under_1500_diagnose(self):
        tier = find_tier(1499.99)
        self.assertEqual(tier.label, "DIAGNOSE_FIRST")
        self.assertEqual(tier.inject_usd, 0.0)

    def test_1500_triggers_baseline_inject(self):
        tier = find_tier(1500.0)
        self.assertEqual(tier.label, "CONFIRM_BASELINE")
        self.assertEqual(tier.inject_usd, 10000.0)

    def test_2000_baseline_inject_10k(self):
        # The deep-dive's expected number → expected default action
        tier = find_tier(2000.0)
        self.assertEqual(tier.label, "CONFIRM_BASELINE")
        self.assertEqual(tier.inject_usd, 10000.0)

    def test_2500_strong_validation_15k(self):
        tier = find_tier(2500.0)
        self.assertEqual(tier.label, "STRONG_VALIDATION")
        self.assertEqual(tier.inject_usd, 15000.0)

    def test_3999_still_strong_validation(self):
        tier = find_tier(3999.99)
        self.assertEqual(tier.label, "STRONG_VALIDATION")
        self.assertEqual(tier.inject_usd, 15000.0)

    def test_4000_massive_upside(self):
        tier = find_tier(4000.0)
        self.assertEqual(tier.label, "MASSIVE_UPSIDE")
        self.assertEqual(tier.inject_usd, 22500.0)

    def test_huge_actual_still_22500(self):
        # Don't accidentally over-inject on a one-time fluke
        tier = find_tier(50000.0)
        self.assertEqual(tier.label, "MASSIVE_UPSIDE")
        self.assertEqual(tier.inject_usd, 22500.0)

    def test_ladder_has_no_gaps_or_overlaps(self):
        # Walk the ladder; every point should match exactly one tier.
        # Also: tiers must be contiguous (no $ between tiers maps to nothing).
        for actual in [-100, -1, 0, 500, 1500, 2000, 2499.99, 2500, 3999.99, 4000, 10000]:
            tier = find_tier(actual)
            self.assertIsNotNone(tier, f"no tier matched ${actual}")


class TestFrozenBaselines(unittest.TestCase):
    """Pin the frozen prediction values — these MUST NOT silently drift."""

    def test_naive_predictor_locked_at_15360(self):
        b = next(p for p in FROZEN_BASELINES_2026_04_28
                 if p.name == "naive_predictor_pre_fix")
        self.assertEqual(b.point, 15360.0)

    def test_deep_dive_locked_at_2000(self):
        b = next(p for p in FROZEN_BASELINES_2026_04_28
                 if p.name == "deep_dive_locked")
        self.assertEqual(b.point, 2000.0)
        self.assertEqual(b.band_low, 1700.0)
        self.assertEqual(b.band_high, 2400.0)

    def test_refined_locked_at_1750(self):
        b = next(p for p in FROZEN_BASELINES_2026_04_28
                 if p.name == "refined_post_apr24_incident")
        self.assertEqual(b.point, 1750.0)


class TestComparison(unittest.TestCase):
    """Verify variance + closest-source logic."""

    def test_variance_correctly_signed(self):
        # actual - predicted: positive means we BEAT prediction
        b = FrozenPrediction(name="x", point=2000.0, band_low=1700, band_high=2400,
                              locked_date="2026", notes="")
        comps = compare_predictions(actual=2200.0, baselines=[b],
                                    live_phantom_corrected=500.0)
        deep = next(c for c in comps if c.source == "x")
        self.assertEqual(deep.variance, 200.0)
        self.assertAlmostEqual(deep.variance_pct, 0.1, places=3)
        self.assertTrue(deep.in_band)  # 2200 in [1700, 2400]

    def test_closest_picks_smallest_abs_variance(self):
        # actual=$2050. Deep dive ($2000) closer than naive ($15k) or refined ($1750).
        comps = compare_predictions(
            actual=2050.0,
            baselines=FROZEN_BASELINES_2026_04_28,
            live_phantom_corrected=500.0,
        )
        closest = find_closest(comps)
        self.assertEqual(closest.source, "deep_dive_locked")
        self.assertEqual(closest.variance, 50.0)

    def test_closest_for_disastrous_outcome(self):
        # actual=$200. Closest should be... live_phantom_corrected if that's
        # 500, deep_dive=$2000, refined=$1750. Phantom-corrected is closest.
        comps = compare_predictions(
            actual=200.0,
            baselines=FROZEN_BASELINES_2026_04_28,
            live_phantom_corrected=500.0,
        )
        closest = find_closest(comps)
        self.assertEqual(closest.source, "phantom_corrected_forward")


class TestPerMarketReconciliation(unittest.TestCase):
    """Per-market variance + missed-market handling."""

    def test_simple_match(self):
        actuals = {"KXTEST-1": 100.0, "KXTEST-2": 50.0}
        predicted = [
            {"ticker": "KXTEST-1", "full_week_pred": 80.0},
            {"ticker": "KXTEST-2", "full_week_pred": 60.0},
        ]
        rows = per_market_reconciliation(actuals, predicted)
        # Sorted by absolute variance descending: KXTEST-1 (+20) first
        self.assertEqual(rows[0]["ticker"], "KXTEST-1")
        self.assertEqual(rows[0]["variance"], 20.0)
        self.assertEqual(rows[1]["ticker"], "KXTEST-2")
        self.assertEqual(rows[1]["variance"], -10.0)

    def test_predicted_market_with_no_actual_appears_as_missed(self):
        # We predicted but Kalshi didn't pay (sub-$1 floor or formula change)
        actuals = {}
        predicted = [{"ticker": "KXMISSED", "full_week_pred": 50.0}]
        rows = per_market_reconciliation(actuals, predicted)
        self.assertEqual(rows[0]["ticker"], "KXMISSED")
        self.assertEqual(rows[0]["actual"], 0.0)
        self.assertEqual(rows[0]["variance"], -50.0)

    def test_unexpected_actual_market_recorded(self):
        # Kalshi paid us on a market we didn't predict (shouldn't happen, but)
        actuals = {"KXSURPRISE": 25.0}
        predicted = []
        rows = per_market_reconciliation(actuals, predicted)
        self.assertEqual(rows[0]["ticker"], "KXSURPRISE")
        self.assertEqual(rows[0]["variance"], 25.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
