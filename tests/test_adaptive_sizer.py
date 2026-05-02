"""Tests for engine/adaptive_sizer.py — pin sizing math + EWMA behavior.

Why critical: sizer decides quote size. Wrong size = wrong share = wrong
rebate. The 2026-04-22 bug (raw size assumed full DF^0=1.0 when often
DF^N applied) was caught manually — these tests prevent regression.

Run: python -m unittest tests.test_adaptive_sizer
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adaptive_sizer import AdaptiveSizer, MarketStats


class TestMarketStats(unittest.TestCase):
    def test_first_observation_initializes_ema(self):
        s = MarketStats()
        s.update(qual_score_excluding_us=100.0, ts=1.0)
        # First sample: alpha=1.0 means EMA = first value
        self.assertEqual(s.ema_qual_score, 100.0)
        self.assertEqual(s.samples, 1)

    def test_ewma_smooths_subsequent_observations(self):
        s = MarketStats()
        s.update(100.0, ts=1.0)  # EMA = 100
        s.update(200.0, ts=2.0)  # EMA = 0.1*200 + 0.9*100 = 110
        self.assertAlmostEqual(s.ema_qual_score, 110.0, places=5)

    def test_recent_window_capped_at_60(self):
        s = MarketStats()
        for i in range(80):
            s.update(float(i), ts=float(i))
        # deque maxlen=60, so window holds last 60 (i=20..79)
        self.assertEqual(len(s.recent_window), 60)

    def test_estimated_competition_uses_window_when_enough(self):
        s = MarketStats()
        for i in range(15):
            s.update(100.0, ts=float(i))
        # >= 10 samples → uses rolling mean
        self.assertEqual(s.estimated_competition(), 100.0)

    def test_estimated_competition_uses_ewma_when_thin(self):
        s = MarketStats()
        s.update(100.0, ts=1.0)  # only 1 sample
        # < 10 samples → uses EWMA
        self.assertEqual(s.estimated_competition(), 100.0)


class TestAdaptiveSizerColdStart(unittest.TestCase):
    """First few snapshots should use defaults, not size from noisy data."""

    def test_no_data_uses_default_size(self):
        sizer = AdaptiveSizer(target_share=0.25)
        size = sizer.size_for("KX-NEW", "yes", target_size_program=500)
        # Returns DEFAULT_QUOTE_SIZE_CONTRACTS (25) capped by fraction
        self.assertGreaterEqual(size, 10)  # MIN_QUOTE_SIZE_CONTRACTS

    def test_two_samples_still_uses_default(self):
        # < 3 samples → cold start
        sizer = AdaptiveSizer(target_share=0.25)
        sizer.observe("KX-NEW", yes_total_qual=100, no_total_qual=100, ts=1.0)
        sizer.observe("KX-NEW", yes_total_qual=100, no_total_qual=100, ts=2.0)
        size = sizer.size_for("KX-NEW", "yes", target_size_program=500)
        self.assertGreaterEqual(size, 10)


class TestAdaptiveSizerMath(unittest.TestCase):
    """Verify the size = S × target/(1-target) formula."""

    def _warm_up(self, sizer: AdaptiveSizer, ticker: str,
                 yes_competition: float, no_competition: float, n: int = 15) -> None:
        # Observe N times with the SAME competition level to converge EMA
        for i in range(n):
            sizer.observe(ticker,
                          yes_total_qual=yes_competition,
                          no_total_qual=no_competition,
                          our_yes_contribution=0.0,
                          our_no_contribution=0.0,
                          ts=float(i))

    def test_target_25pct_share_with_thin_competition(self):
        # Competition = 100; target_share = 0.25
        # Expected: our_size = 100 × 0.25/0.75 = 33.33 ≈ 33
        sizer = AdaptiveSizer(target_share=0.25)
        self._warm_up(sizer, "KX-THIN", yes_competition=100.0, no_competition=100.0)
        size = sizer.size_for("KX-THIN", "yes", target_size_program=500)
        # Allow some range due to EWMA smoothing
        self.assertGreaterEqual(size, 25)
        self.assertLessEqual(size, 45)

    def test_target_25pct_share_with_thick_competition(self):
        # Competition = 3000; target_share = 0.25
        # Expected: 3000 × 0.33 = 1000, but cap = MIN_QUOTE_SIZE_CONTRACTS × 20
        # 2026-05-02: was hardcoded to 200; uses settings now (MIN bumped 10→25)
        from config import settings
        sizer = AdaptiveSizer(target_share=0.25)
        self._warm_up(sizer, "KX-THICK", yes_competition=3000.0, no_competition=3000.0)
        size = sizer.size_for("KX-THICK", "yes", target_size_program=2500)
        self.assertEqual(size, settings.MIN_QUOTE_SIZE_CONTRACTS * 20)

    def test_higher_target_share_increases_size(self):
        thin = AdaptiveSizer(target_share=0.10)
        big  = AdaptiveSizer(target_share=0.50)
        for s in (thin, big):
            self._warm_up(s, "KX", yes_competition=200.0, no_competition=200.0)
        thin_size = thin.size_for("KX", "yes", 500)
        big_size = big.size_for("KX", "yes", 500)
        # 0.50 share asks for more contracts than 0.10
        self.assertGreater(big_size, thin_size)


class TestObserveSubtractsOurContribution(unittest.TestCase):
    """The 2026-04-22 fix: feed observe() the discount-adjusted contribution
    so it learns from REAL competition, not inflated total."""

    def test_competition_excludes_our_contribution(self):
        # Total = 200, we contribute 50 → competition = 150
        sizer = AdaptiveSizer(target_share=0.25)
        sizer.observe("KX", yes_total_qual=200.0, no_total_qual=200.0,
                      our_yes_contribution=50.0, our_no_contribution=50.0, ts=1.0)
        # ema_qual_score = 200 - 50 = 150
        self.assertEqual(sizer.yes_stats["KX"].ema_qual_score, 150.0)

    def test_negative_competition_clamped_to_zero(self):
        # If our contribution > total (math edge case), don't go negative
        sizer = AdaptiveSizer(target_share=0.25)
        sizer.observe("KX", yes_total_qual=50.0, no_total_qual=50.0,
                      our_yes_contribution=100.0, our_no_contribution=100.0, ts=1.0)
        self.assertGreaterEqual(sizer.yes_stats["KX"].ema_qual_score, 0)


class TestReportFunction(unittest.TestCase):
    def test_report_includes_observed_markets(self):
        sizer = AdaptiveSizer()
        sizer.observe("KX-A", yes_total_qual=100, no_total_qual=100, ts=1.0)
        sizer.observe("KX-B", yes_total_qual=200, no_total_qual=200, ts=2.0)
        report = sizer.report()
        self.assertIn("KX-A", report)
        self.assertIn("KX-B", report)
        self.assertIn("yes_competition_ema", report["KX-A"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
