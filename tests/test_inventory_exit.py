"""P4b regressions — inventory must not be held to settlement by default
(2026-09-21).

run_paper.py had no liquidate/unwind/flatten/exit logic at all; inventory
acquired by a fill was held until the market settled. Passive pairing existed
only in research/maker_replay.py, which the runner never imports. A LIP maker
earns a bounded rebate and takes unbounded directional risk in exchange, so
holding to settlement turns market-making into unsized directional bets.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import fees
from engine.inventory_exit import (
    ExitPolicy, Position, reduces_exposure,
)

D = Decimal
NOW = 1_800_000_000.0


def _pos(yes=0, no=0, age_sec=0.0):
    return Position("KXTEST-1", D(str(yes)), D(str(no)), NOW - age_sec)


def _eval(pos, policy=None, yes_bid=49, no_bid=49, schedule=None, now=NOW):
    p = policy or ExitPolicy()
    return p.evaluate(pos, now=now, best_yes_bid_cents=yes_bid,
                      best_no_bid_cents=no_bid, fee_schedule=schedule)


class TestNetExposure:
    def test_matched_pairs_are_not_exposure(self):
        p = _pos(yes=100, no=100, age_sec=999999)
        assert p.net_yes == 0 and p.paired == 100
        assert p.exposed_side is None
        assert not _eval(p)          # riskless: nothing to exit

    def test_net_long_yes(self):
        p = _pos(yes=30, no=10)
        assert p.net_yes == 20 and p.exposed_side == "yes" and p.paired == 10

    def test_net_long_no(self):
        p = _pos(yes=5, no=25)
        assert p.net_yes == -20 and p.exposed_side == "no"


class TestAgeRule:
    def test_young_small_position_is_held(self):
        d = _eval(_pos(yes=5, age_sec=60))
        assert not d and "within limits" in d.reason

    def test_old_position_exits(self):
        d = _eval(_pos(yes=5, age_sec=3601))
        assert d and d.reason == "max_holding_age"

    def test_exit_buys_the_opposite_side_to_complete_pairs(self):
        """Reducing a long-YES net means BUYING NO — which can be done
        passively at the bid we are already quoting."""
        d = _eval(_pos(yes=5, age_sec=3601))
        assert d.side == "no" and d.qty == 5

    def test_long_no_exit_buys_yes(self):
        d = _eval(_pos(no=5, age_sec=3601))
        assert d.side == "yes" and d.qty == 5

    def test_passive_before_the_aggressive_deadline(self):
        d = _eval(_pos(yes=5, age_sec=3601), yes_bid=49, no_bid=48)
        assert not d.aggressive and d.limit_price_cents == 48   # join the bid

    def test_aggressive_after_the_deadline(self):
        d = _eval(_pos(yes=5, age_sec=7201), yes_bid=49, no_bid=48)
        assert d.aggressive and d.limit_price_cents == 49       # pay up a cent
        assert d.reason.endswith("_aggressive")


class TestSizeRule:
    def test_large_net_exits_even_when_young(self):
        d = _eval(_pos(yes=100, age_sec=1))
        assert d and d.reason == "max_net_exposure"

    def test_at_the_threshold_is_held(self):
        d = _eval(_pos(yes=25, age_sec=1))
        assert not d

    def test_just_over_the_threshold_exits(self):
        assert _eval(_pos(yes=26, age_sec=1))

    def test_custom_policy_thresholds(self):
        strict = ExitPolicy(max_holding_sec=10, max_net_contracts=D("2"))
        assert _eval(_pos(yes=3, age_sec=1), strict)
        assert _eval(_pos(yes=1, age_sec=11), strict)


class TestCostAwareness:
    def test_tiny_exposure_at_an_extreme_price_not_worth_the_fee(self):
        """Rule 3: never pay more to exit than the exposure is worth.

        1 contract with the reducing side bid at 2c: the round-up makes the
        fee 1c against 2c of exposure — half the position's value to close
        it. Holding is cheaper."""
        d = _eval(_pos(yes=1, age_sec=3601), no_bid=2,
                  schedule=fees.KALSHI_UNVERIFIED)
        assert not d and "holding is cheaper" in d.reason

    def test_small_exposure_at_mid_price_is_still_worth_exiting(self):
        """4c of fee against 50c of exposure is cheap; don't over-block."""
        d = _eval(_pos(yes=1, age_sec=3601), no_bid=49,
                  schedule=fees.KALSHI_UNVERIFIED)
        assert d

    def test_meaningful_exposure_is_worth_exiting(self):
        d = _eval(_pos(yes=200, age_sec=99999), schedule=fees.KALSHI_UNVERIFIED)
        assert d and d.estimated_cost_usd > 0

    def test_cost_includes_the_fee(self):
        no_fee = _eval(_pos(yes=200, age_sec=3601))
        with_fee = _eval(_pos(yes=200, age_sec=3601),
                         schedule=fees.KALSHI_UNVERIFIED)
        assert with_fee.estimated_cost_usd > no_fee.estimated_cost_usd

    def test_fee_schedule_failure_does_not_block_an_exit(self):
        class Broken:
            def fee_usd(self, *a, **k):
                raise RuntimeError("boom")
        assert _eval(_pos(yes=200, age_sec=3601), schedule=Broken())


class TestSafety:
    def test_no_book_means_no_exit(self):
        d = _eval(_pos(yes=5, age_sec=99999), no_bid=None)
        assert not d and "cannot price an exit" in d.reason

    def test_aggressive_price_that_would_reach_one_hundred_is_refused(self):
        # bid 99 + 1c aggression = 100c, which the venue rejects.
        d = _eval(_pos(yes=5, age_sec=99999), no_bid=99)
        assert not d and "off-grid" in d.reason

    def test_passive_price_of_zero_is_refused(self):
        # Age past max_holding but before the aggressive deadline: limit
        # stays at the 0c bid, which is not a valid order price.
        d = _eval(_pos(yes=5, age_sec=3601), no_bid=0)
        assert not d and "off-grid" in d.reason

    def test_flat_position_never_exits(self):
        assert not _eval(_pos(yes=0, no=0, age_sec=99999))


class TestReducesExposureGuard:
    def test_buying_the_opposite_side_reduces(self):
        assert reduces_exposure(_pos(yes=10), "no", 5)
        assert reduces_exposure(_pos(no=10), "yes", 5)

    def test_buying_the_same_side_increases_and_is_rejected(self):
        """The unwind-vs-double-down distinction, checked not assumed."""
        assert not reduces_exposure(_pos(yes=10), "yes", 5)
        assert not reduces_exposure(_pos(no=10), "no", 5)

    def test_overshoot_that_grows_the_other_way_is_rejected(self):
        # long 10 YES; buying 25 NO leaves net -15, worse than 10.
        assert not reduces_exposure(_pos(yes=10), "no", 25)

    def test_overshoot_to_equal_and_opposite_is_not_a_reduction(self):
        """|10 - 20| == 10, the same exposure pointing the other way. The
        guard is strict: no change is not an improvement."""
        assert not reduces_exposure(_pos(yes=10), "no", 20)

    def test_exact_flatten_is_a_reduction(self):
        assert reduces_exposure(_pos(yes=10), "no", 10)

    def test_zero_or_negative_quantity_rejected(self):
        assert not reduces_exposure(_pos(yes=10), "no", 0)
        assert not reduces_exposure(_pos(yes=10), "no", -5)

    def test_invalid_side_rejected(self):
        assert not reduces_exposure(_pos(yes=10), "maybe", 5)

    def test_every_policy_exit_passes_the_guard(self):
        """Whatever the policy proposes must actually reduce exposure."""
        for yes, no, age in [(100, 0, 99999), (0, 100, 99999), (60, 20, 10),
                             (5, 40, 4000)]:
            p = _pos(yes=yes, no=no, age_sec=age)
            d = _eval(p, schedule=fees.KALSHI_UNVERIFIED)
            if d:
                assert reduces_exposure(p, d.side, d.qty), (yes, no, age)
