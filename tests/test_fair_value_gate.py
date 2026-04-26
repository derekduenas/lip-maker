"""Tests for run_paper.PaperRunner._fair_value_skip — the futures gate.

Why critical: Apr 24 2026 lost -$66.75 on Coffee because confidence='unreliable'
made the gate skip. These tests pin both the working ("exact"/"close" with
sufficient delta = SKIP) and broken ("unreliable" = no skip) behaviors.

Task #102 will add unreliable-feed handling — when that ships, ADD tests
for the new behavior, don't replace existing ones.

Run: python -m unittest tests.test_fair_value_gate
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing run_paper triggers heavy WS imports — defer to test methods if needed
from run_paper import PaperRunner


def _runner_with_futures(prefix: str, price: float) -> PaperRunner:
    """Build a PaperRunner with a pre-populated futures cache. Skips the
    DB query path so tests are fast + deterministic."""
    runner = PaperRunner(markets=[])
    runner._futures_cache[prefix] = (price, time.time())
    runner._futures_cache_ts = time.time()  # prevent refresh
    return runner


class TestFairValueSkipExact(unittest.TestCase):
    """Pin gate behavior for exact-confidence prefixes (Brent, NatGas, Copper, etc.)."""

    def test_skip_when_futures_above_strike_and_no_bid_high(self):
        # Brent futures $103, strike $98 → futures > strike + 5 (>3% threshold).
        # Our NO bid at 50c is too high → SKIP.
        r = _runner_with_futures("KXBRENTD", 103.0)
        result = r._fair_value_skip("KXBRENTD-26APR2317-T98", yes_bid=50, no_bid=50)
        self.assertIsNotNone(result)
        self.assertIn("NO worthless", result)

    def test_no_skip_when_futures_close_to_strike(self):
        # Brent $103.5, strike $103 → delta only 0.5, below 3% threshold of $3.09
        r = _runner_with_futures("KXBRENTD", 103.5)
        result = r._fair_value_skip("KXBRENTD-26APR2317-T103", yes_bid=50, no_bid=50)
        self.assertIsNone(result)  # within threshold = OK to quote

    def test_no_skip_when_no_bid_low_enough(self):
        # Even with futures clearly above strike, if our NO bid is cheap (≤30c)
        # we accept the directional risk
        r = _runner_with_futures("KXBRENTD", 110.0)
        result = r._fair_value_skip("KXBRENTD-26APR2317-T98", yes_bid=80, no_bid=20)
        self.assertIsNone(result)

    def test_skip_when_futures_below_strike_and_yes_bid_high(self):
        # Mirror case: futures BELOW strike → NO will win → don't post YES at high price
        r = _runner_with_futures("KXBRENTD", 95.0)
        result = r._fair_value_skip("KXBRENTD-26APR2317-T103", yes_bid=50, no_bid=50)
        self.assertIsNotNone(result)
        self.assertIn("YES worthless", result)


class TestFairValueSkipUnreliableConfidence(unittest.TestCase):
    """Pin the CURRENT behavior: unreliable feeds get skipped (no gate fires).
    This is the Apr 24 Coffee bug. Task #102 will add stricter behavior for
    unreliable feeds — when shipped, those tests get added separately."""

    def test_unreliable_coffee_does_not_fire_gate(self):
        # Coffee marked 'unreliable' in FUTURES_MAP. Even with massive delta,
        # the gate currently skips entirely. Apr 24 lost -$66 because of this.
        r = _runner_with_futures("KXCOFFEEW", 350.0)
        # Strike 290 — futures 60 above strike (huge directional signal)
        result = r._fair_value_skip("KXCOFFEEW-26APR2417-T290", yes_bid=80, no_bid=80)
        # Currently returns None (skip) — task #102 should make it return a skip
        self.assertIsNone(result)

    def test_unreliable_sugar_does_not_fire_gate(self):
        r = _runner_with_futures("KXSUGARW", 14.10)
        result = r._fair_value_skip("KXSUGARW-26APR2417-T13.74", yes_bid=80, no_bid=80)
        self.assertIsNone(result)


class TestFairValueSkipEdgeCases(unittest.TestCase):
    def test_unmapped_prefix_returns_none(self):
        # KXEOWEEK is not in FUTURES_MAP
        r = _runner_with_futures("KXBRENTD", 100.0)
        result = r._fair_value_skip("KXEOWEEK-26APR25-0", yes_bid=50, no_bid=50)
        self.assertIsNone(result)

    def test_no_strike_in_ticker_returns_none(self):
        # Ticker without -T<strike> suffix
        r = _runner_with_futures("KXBRENTD", 100.0)
        result = r._fair_value_skip("KXBRENTD-26APR2317", yes_bid=50, no_bid=50)
        self.assertIsNone(result)

    def test_no_cached_futures_price_returns_none(self):
        # Cache empty for the prefix
        r = PaperRunner(markets=[])
        # Manually prevent the DB refresh attempt by setting cache_ts to now
        r._futures_cache_ts = time.time()
        result = r._fair_value_skip("KXBRENTD-26APR2317-T100", yes_bid=50, no_bid=50)
        self.assertIsNone(result)

    def test_threshold_floor_is_one_dollar(self):
        # For tiny strikes, the 3% threshold falls below $1.0; floor at $1.0
        # NatGas strike 2.50, futures 2.55: delta=+0.05, 3% threshold=0.075 BUT floor=1.0
        # → delta < threshold(1.0) → no skip
        r = _runner_with_futures("KXNATGASD", 2.55)
        result = r._fair_value_skip("KXNATGASD-26APR2317-T2.50", yes_bid=80, no_bid=80)
        self.assertIsNone(result)


class TestFairValueSkipRegression(unittest.TestCase):
    """Anchor specific historical scenarios so we don't regress."""

    def test_apr_24_brent_t103_50_was_at_threshold(self):
        # Apr 24: KXBRENTD-T103.50, futures ~106. delta=2.5, threshold=max(3.105, 1.0)=3.105
        # delta < threshold → gate didn't fire (correctly — it's marginal)
        r = _runner_with_futures("KXBRENTD", 106.0)
        result = r._fair_value_skip("KXBRENTD-26APR2317-T103.50", yes_bid=50, no_bid=50)
        # Currently no skip (delta=2.5 < threshold=3.105). This test pins
        # WHY we lost money here — gate threshold may need tuning (task #102 area).
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
