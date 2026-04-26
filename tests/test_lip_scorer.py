"""Golden-vector regression tests for engine/lip_scorer.py.

Why this exists: Kalshi changed the LIP formula via amendment Feb 28 2026
(added the two-sided requirement). Without these tests, the next amendment
silently produces zero rebate — only detectable on the Monday weekly payout,
i.e., a week late. These tests pin the math down so any formula change
breaks loudly in CI/dev rather than silently in production.

Run: python -m unittest tests.test_lip_scorer

If any test fails, DO NOT mute it — figure out whether:
  (a) Kalshi changed the formula (update the spec + test)
  (b) Our implementation drifted (fix the bug)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.lip_scorer import (
    OurQuotes,
    ProgramParams,
    SnapshotScore,
    _find_cutoff_price,
    _score_bids,
    score_snapshot,
)
from execution.kalshi_ws import BookLevel, BookState


def _params(**kw) -> ProgramParams:
    """Helper: ProgramParams with sensible defaults."""
    defaults = dict(
        market_ticker="TEST",
        target_size=100,
        discount_factor=0.5,
        period_reward_usd=100.0,
    )
    defaults.update(kw)
    return ProgramParams(**defaults)


class TestCutoffCalculation(unittest.TestCase):
    """Verify _find_cutoff_price walks bids correctly."""

    def test_first_level_meets_target(self):
        bids = [BookLevel(50, 200)]  # 200 contracts at 50c
        cutoff = _find_cutoff_price(bids, target_size=100)
        self.assertEqual(cutoff, 50)

    def test_accumulation_across_levels(self):
        bids = [BookLevel(50, 30), BookLevel(49, 80)]  # 30 + 80 = 110 ≥ 100
        cutoff = _find_cutoff_price(bids, target_size=100)
        self.assertEqual(cutoff, 49)

    def test_book_too_thin_returns_none(self):
        bids = [BookLevel(50, 30), BookLevel(49, 30)]  # 60 < 100
        cutoff = _find_cutoff_price(bids, target_size=100)
        self.assertIsNone(cutoff)

    def test_empty_book_returns_none(self):
        self.assertIsNone(_find_cutoff_price([], target_size=100))


class TestScoreBids(unittest.TestCase):
    """Verify the discount-factor power calculation per Kalshi formula."""

    def test_at_reference_price_full_score(self):
        # DF^0 × size = size
        all_bids = [BookLevel(50, 100)]
        ours = [BookLevel(50, 100)]
        our_score, total = _score_bids(all_bids, ours, reference_price=50,
                                        discount_factor=0.5, cutoff_price=50)
        self.assertEqual(our_score, 100.0)  # 0.5^0 × 100
        self.assertEqual(total, 100.0)

    def test_one_tick_back_half_score(self):
        # DF=0.5, 1 tick back: 0.5^1 × size = 0.5 × size
        all_bids = [BookLevel(49, 100)]
        ours = [BookLevel(49, 100)]
        our_score, total = _score_bids(all_bids, ours, reference_price=50,
                                        discount_factor=0.5, cutoff_price=49)
        self.assertEqual(our_score, 50.0)  # 0.5^1 × 100

    def test_two_ticks_back_quarter_score(self):
        # DF=0.5, 2 ticks back: 0.5^2 × size = 0.25 × size
        all_bids = [BookLevel(48, 100)]
        ours = [BookLevel(48, 100)]
        our_score, total = _score_bids(all_bids, ours, reference_price=50,
                                        discount_factor=0.5, cutoff_price=48)
        self.assertEqual(our_score, 25.0)  # 0.5^2 × 100

    def test_below_cutoff_excluded(self):
        # Level at 48c is below cutoff 49 — should not score
        all_bids = [BookLevel(50, 80), BookLevel(48, 100)]
        ours = [BookLevel(48, 100)]
        our_score, total = _score_bids(all_bids, ours, reference_price=50,
                                        discount_factor=0.5, cutoff_price=49)
        self.assertEqual(our_score, 0.0)


class TestSnapshotValidity(unittest.TestCase):
    """Verify the Feb 28 2026 two-sided requirement is enforced."""

    def test_two_sided_valid_when_both_meet_target(self):
        book = BookState(market_ticker="TEST")
        book.yes_bids = [BookLevel(50, 100)]
        book.no_bids  = [BookLevel(50, 100)]
        result = score_snapshot(book, OurQuotes(), _params(target_size=100))
        self.assertTrue(result.snapshot_valid)
        self.assertTrue(result.yes_qualified)
        self.assertTrue(result.no_qualified)

    def test_invalid_when_yes_side_fails_target(self):
        book = BookState(market_ticker="TEST")
        book.yes_bids = [BookLevel(50, 50)]   # < 100
        book.no_bids  = [BookLevel(50, 100)]
        result = score_snapshot(book, OurQuotes(), _params(target_size=100))
        self.assertFalse(result.snapshot_valid)
        self.assertFalse(result.yes_qualified)
        self.assertTrue(result.no_qualified)
        self.assertEqual(result.our_total_score, 0.0)

    def test_invalid_when_no_side_fails_target(self):
        book = BookState(market_ticker="TEST")
        book.yes_bids = [BookLevel(50, 100)]
        book.no_bids  = [BookLevel(50, 50)]   # < 100
        result = score_snapshot(book, OurQuotes(), _params(target_size=100))
        self.assertFalse(result.snapshot_valid)
        self.assertEqual(result.our_total_score, 0.0)


class TestSharedNormalization(unittest.TestCase):
    """Verify our normalized share against known total scores."""

    def test_alone_on_both_sides_full_score(self):
        # We're the only LP at the reference price on both sides
        book = BookState(market_ticker="TEST")
        book.yes_bids = [BookLevel(50, 100)]
        book.no_bids  = [BookLevel(50, 100)]
        ours = OurQuotes(
            yes_bids=[BookLevel(50, 100)],
            no_bids=[BookLevel(50, 100)],
        )
        # Edge: when "we are the book", scorer's total includes our quotes
        # (because all_bids = book.yes_bids which contains us). Our share = 1.
        result = score_snapshot(book, ours, _params(target_size=100))
        self.assertTrue(result.snapshot_valid)
        self.assertEqual(result.our_yes_normalized, 1.0)
        self.assertEqual(result.our_no_normalized, 1.0)
        self.assertEqual(result.our_total_score, 2.0)

    def test_half_share_when_competitor_equal(self):
        # Competitor at same price/size on both sides -> 50% share each side
        book = BookState(market_ticker="TEST")
        book.yes_bids = [BookLevel(50, 200)]  # 100 ours + 100 competitor
        book.no_bids  = [BookLevel(50, 200)]
        ours = OurQuotes(
            yes_bids=[BookLevel(50, 100)],
            no_bids=[BookLevel(50, 100)],
        )
        result = score_snapshot(book, ours, _params(target_size=100))
        self.assertTrue(result.snapshot_valid)
        self.assertEqual(result.our_yes_normalized, 0.5)
        self.assertEqual(result.our_no_normalized, 0.5)
        self.assertEqual(result.our_total_score, 1.0)

    def test_one_tick_back_share_with_thin_competitor(self):
        # Realistic case: competitor at 50c size 50 (alone, doesn't meet target).
        # We at 49c size 100. Combined book reaches target only at 49c.
        # Cutoff = 49.
        # Total = 0.5^0 × 50 + 0.5^1 × 100 = 50 + 50 = 100
        # Ours  = 0.5^1 × 100 = 50
        # Share = 50/100 = 0.5
        # NOTE: if competitor at 50c alone meets target, cutoff = 50 and we
        # at 49c get ZERO score (we're below cutoff). This test pins down
        # the case where our 1-tick-back position actually qualifies.
        book = BookState(market_ticker="TEST")
        book.yes_bids = [BookLevel(50, 50), BookLevel(49, 100)]
        book.no_bids  = [BookLevel(50, 50), BookLevel(49, 100)]
        ours = OurQuotes(
            yes_bids=[BookLevel(49, 100)],
            no_bids=[BookLevel(49, 100)],
        )
        result = score_snapshot(book, ours, _params(target_size=100, discount_factor=0.5))
        self.assertTrue(result.snapshot_valid)
        self.assertEqual(result.yes_cutoff_price, 49)
        self.assertAlmostEqual(result.our_yes_normalized, 0.5, places=5)
        self.assertAlmostEqual(result.our_no_normalized, 0.5, places=5)
        self.assertAlmostEqual(result.our_total_score, 1.0, places=5)

    def test_below_cutoff_returns_zero_share(self):
        # CRITICAL pin: if competitor at best alone meets target, our quote
        # one tick back is BELOW cutoff and scores 0. This is the "lone
        # qualifier" trap — we think we're providing liquidity but Kalshi
        # excludes us. Ship guardrail: don't quote one tick back unless
        # competitor depth is THIN enough that cutoff falls to our level.
        book = BookState(market_ticker="TEST")
        book.yes_bids = [BookLevel(50, 200), BookLevel(49, 100)]  # comp meets target alone
        book.no_bids  = [BookLevel(50, 200), BookLevel(49, 100)]
        ours = OurQuotes(
            yes_bids=[BookLevel(49, 100)],
            no_bids=[BookLevel(49, 100)],
        )
        result = score_snapshot(book, ours, _params(target_size=100, discount_factor=0.5))
        self.assertTrue(result.snapshot_valid)
        self.assertEqual(result.yes_cutoff_price, 50)  # cutoff at competitor level
        self.assertEqual(result.our_yes_normalized, 0.0)
        self.assertEqual(result.our_total_score, 0.0)


class TestRegressionSpecifics(unittest.TestCase):
    """Pin down specific behaviors that have caused bugs in this codebase."""

    def test_invalid_snapshot_returns_zero_qualifying_score_fields(self):
        # Bug guard: if scorer returns invalid, downstream code reads
        # yes_total_qualifying_score / no_total_qualifying_score and assumes
        # zero. Verify they ARE zero when invalid.
        book = BookState(market_ticker="TEST")
        book.yes_bids = []  # empty -> invalid
        book.no_bids  = [BookLevel(50, 100)]
        result = score_snapshot(book, OurQuotes(), _params())
        self.assertEqual(result.yes_total_qualifying_score, 0.0)
        self.assertEqual(result.no_total_qualifying_score, 0.0)

    def test_our_total_score_capped_at_2(self):
        # Mathematical invariant: our_normalized per side ∈ [0,1],
        # so our_total_score ∈ [0,2]. Pin this.
        book = BookState(market_ticker="TEST")
        book.yes_bids = [BookLevel(50, 100)]
        book.no_bids  = [BookLevel(50, 100)]
        ours = OurQuotes(
            yes_bids=[BookLevel(50, 100)],
            no_bids=[BookLevel(50, 100)],
        )
        result = score_snapshot(book, ours, _params())
        self.assertLessEqual(result.our_total_score, 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
