"""P4 regressions — one fee schedule, with provenance, and the round-up
actually applied (2026-09-21).

Four incompatible models coexisted and the most optimistic fed the headline
metric: tools/net_yield_logger.py set `fees = 0.0` with the uncited comment
"LIP maker orders are fee-free per Kalshi", while research/maker_replay.py
used $0.01/$0.02 per contract and dislocation/config.py used 7% of value.
Separately, dislocation/spread.py documented a per-fill ceiling to the next
whole cent that was implemented nowhere — and for small maker fills that
ceiling is the dominant term.
"""
from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import fees
from engine.fees import (
    ASSUME_FREE_MAKER, KALSHI_UNVERIFIED, FeeSchedule, UnverifiedFeeSchedule,
    fee_usd, provenance_warning, round_trip_usd,
)

D = Decimal


@pytest.fixture(autouse=True)
def _restore_schedule():
    original = fees.active_schedule()
    yield
    fees.set_schedule(original)


class TestFormula:
    def test_documented_form_at_fifty_cents(self):
        # 0.07 x 10 x 0.5 x 0.5 = $0.175, and ceil to $0.000001 leaves it.
        assert fee_usd(50, 10) == D("0.1750")

    def test_quadratic_vanishes_at_the_edges(self):
        """P(1-P) is largest at 50c and ~0 near 0 and 100."""
        mid = KALSHI_UNVERIFIED.fee_usd(50, 1000)
        edge = KALSHI_UNVERIFIED.fee_usd(2, 1000)
        assert mid > edge > 0

    @pytest.mark.parametrize("price", [0, 100])
    def test_no_fee_at_price_extremes(self, price):
        assert fee_usd(price, 100) == D("0")

    def test_zero_or_negative_contracts_free(self):
        assert fee_usd(50, 0) == D("0")
        assert fee_usd(50, -5) == D("0")


class TestRounding:
    """VERIFIED 2026-09-20 against docs.kalshi.com/getting_started/fee_rounding:
    the trade fee is ceil_6dp(model_fee) — rounded up to the nearest
    $0.000001. NOT to the next whole cent.

    This repo briefly implemented ceil-to-cent from a code comment. For the
    small fills a LIP maker actually gets, that rounding IS the fee: one
    contract at 5c costs $0.003325, which cent-rounding inflates to $0.01,
    a 3x overstatement. The repo had swung from understating fees to
    overstating them, and both distort the decision to quote.
    """

    def test_default_rounds_to_a_millionth_of_a_dollar(self):
        assert KALSHI_UNVERIFIED.describe()["rounding"] == "ceil_6dp"

    def test_ceiling_is_applied_at_six_decimal_places(self):
        # 1 contract @5c: 0.07 * 0.05 * 0.95 = $0.003325 exactly.
        assert fee_usd(5, 1) == D("0.003325")

    def test_a_sub_micro_fee_is_rounded_up_not_down(self):
        tiny = FeeSchedule(name="t", rate=D("0.0000001"), source="test")
        f = tiny.fee_usd(50, 1)
        assert f > 0, "a non-zero fee was rounded away to nothing"
        assert f == D("0.000001")

    def test_cent_rounding_is_available_but_is_not_the_venue_rule(self):
        """Kept only so the wrong assumption can be A/B'd against."""
        cent = FeeSchedule(name="t", rate=D("0.07"), source="test",
                           round_up_to_cent=True)
        assert cent.fee_usd(5, 1) == D("0.01")
        assert cent.fee_usd(5, 1) > KALSHI_UNVERIFIED.fee_usd(5, 1)

    def test_unrounded_schedule_differs_only_below_a_millionth(self):
        raw = FeeSchedule(name="t", rate=D("0.07"), source="test",
                          rounding="none")
        assert raw.fee_usd(50, 1) == D("0.0175")
        assert KALSHI_UNVERIFIED.fee_usd(50, 1) == D("0.0175")


class TestProvenance:
    def test_rate_is_still_not_verified(self):
        """Rounding and maker-charging are verified; the RATE is not, and a
        conservative assumption must not be promoted to a verified fact."""
        assert KALSHI_UNVERIFIED.verified is False
        assert "RATE unverified" in KALSHI_UNVERIFIED.source

    def test_require_verified_refuses_to_guess(self):
        with pytest.raises(UnverifiedFeeSchedule, match="unverified"):
            fee_usd(50, 10, require_verified=True)
        with pytest.raises(UnverifiedFeeSchedule):
            round_trip_usd(50, 50, 10, require_verified=True)

    def test_verified_schedule_satisfies_the_requirement(self):
        fees.set_schedule(FeeSchedule(name="confirmed", rate=D("0.07"),
                                      source="operator confirmed", verified=True,
                                      verified_at="2026-09-21"))
        assert fee_usd(50, 10, require_verified=True) == D("0.1750")
        assert provenance_warning() is None

    def test_warning_names_the_schedule(self):
        w = provenance_warning()
        assert "UNVERIFIED" in w and "kalshi_documented_unverified" in w

    def test_describe_carries_the_audit_fields(self):
        d = KALSHI_UNVERIFIED.describe()
        assert d["verified"] is False and d["rounding"] == "ceil_6dp"
        assert d["charge_maker"] is True and d["source"]


class TestMakerAssumption:
    def test_makers_are_charged_and_this_is_verified(self):
        """VERIFIED against help.kalshi.com: 'Maker fees are charged for
        orders placed that are not immediately matched and are instead left
        as resting orders on the orderbook.' This refutes the repo's uncited
        'LIP maker orders are fee-free per Kalshi'."""
        assert KALSHI_UNVERIFIED.charge_maker is True
        assert fee_usd(50, 10, is_taker=False) > 0

    def test_legacy_free_maker_schedule_available_for_ab(self):
        fees.set_schedule(ASSUME_FREE_MAKER)
        assert fee_usd(50, 10, is_taker=False) == D("0")
        assert fee_usd(50, 10, is_taker=True) == D("0.1750")

    def test_round_trip_charges_entry_and_exit(self):
        rt = round_trip_usd(50, 50, 10)
        assert rt == D("0.3500")     # maker in, taker out


class TestNetYieldNoLongerAssumesZero:
    def _db(self, tmp_path, fills):
        path = tmp_path / "f.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE fill_ledger (
                trade_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT, side TEXT,
                count INTEGER, count_real REAL, yes_price_cents INTEGER,
                no_price_cents INTEGER, is_taker INTEGER, created_at TEXT,
                synced_at TEXT);
        """)
        for i, (side, n, yp, npc, taker) in enumerate(fills):
            conn.execute("INSERT INTO fill_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (f"t{i}", "o", "KX", side, int(n), float(n), yp, npc,
                          taker, "2026-09-20T12:00:00Z", "2026-09-20"))
        conn.commit()
        return conn

    def test_day_fees_are_nonzero_for_real_fills(self, tmp_path):
        from tools.net_yield_logger import _day_fees_usd
        conn = self._db(tmp_path, [("yes", 10, 50, 50, 0), ("no", 5, 40, 60, 0)])
        total = _day_fees_usd(conn, "2026-09-20")
        assert total > 0

    def test_fees_are_computed_per_fill_not_on_a_summed_position(self, tmp_path):
        """Each fill is charged separately. Under the VERIFIED ceil_6dp rule
        that happens to agree with charging the block at ordinary prices —
        the two diverged only under the cent-rounding this repo briefly and
        wrongly used. The property being pinned is that the logger iterates
        fills; the arithmetic agreeing here is a fact about the rule, not an
        excuse to sum contracts first."""
        from tools.net_yield_logger import _day_fees_usd
        singles = self._db(tmp_path, [("yes", 1, 50, 50, 0)] * 10)
        assert _day_fees_usd(singles, "2026-09-20") == D("0.17500")

    def test_per_fill_rounding_still_bites_below_a_millionth(self):
        """Where the ceiling does apply, ten small fills cost more than one
        block — so summing first would still understate."""
        tiny = FeeSchedule(name="t", rate=D("0.0000001"), source="test")
        ten = sum((tiny.fee_usd(50, 1) for _ in range(10)), D(0))
        one = tiny.fee_usd(50, 10)
        assert ten > one

    def test_other_days_excluded(self, tmp_path):
        from tools.net_yield_logger import _day_fees_usd
        conn = self._db(tmp_path, [("yes", 10, 50, 50, 0)])
        assert _day_fees_usd(conn, "2026-09-19") == D("0")

    def test_missing_ledger_is_tolerated(self, tmp_path):
        from tools.net_yield_logger import _day_fees_usd
        conn = sqlite3.connect(tmp_path / "empty.db")
        assert _day_fees_usd(conn, "2026-09-20") == D("0")

    def test_free_maker_schedule_reproduces_the_old_zero(self, tmp_path):
        """Quantifies how much of the old net-yield number was the
        assumption rather than the trading."""
        from tools.net_yield_logger import _day_fees_usd
        conn = self._db(tmp_path, [("yes", 10, 50, 50, 0)])
        charged = _day_fees_usd(conn, "2026-09-20")
        fees.set_schedule(ASSUME_FREE_MAKER)
        assert _day_fees_usd(conn, "2026-09-20") == D("0")
        assert charged > 0


class TestVenueFeePipeline:
    """The documented pipeline has FOUR stages, and ceil_6dp is only the
    first. VERIFIED against docs.kalshi.com/getting_started/fee_rounding,
    including its worked example.

        1. trade_fee      = ceil_6dp(model_fee)
        2. aligned_change = floor_precision(revenue - trade_fee)
        3. rounding_fee   = (revenue - trade_fee) - aligned_change
        4. net_fee        = trade_fee + rounding_fee - rebate, floored at 0

    Stage 3 is why a blanket substitution of microdollars for cents is
    wrong in the other direction: flooring the BALANCE change to a cent
    means a non-direct member can pay nearly a cent more than the trade fee
    on one fill. Stage 4 is what keeps that from being permanent.
    """

    def test_reproduces_the_documented_worked_example(self):
        from engine.fees import OrderFeeAccumulator
        acc = OrderFeeAccumulator()
        r = acc.charge(revenue_usd=D("-0.055000"), model_fee_usd=D("0.00363825"))
        assert r.trade_fee_usd == D("0.003639")
        assert r.aligned_change_usd == D("-0.06")
        assert r.rounding_fee_usd == D("0.001361")
        assert r.trade_fee_usd + r.rounding_fee_usd == D("0.005000")

    def test_trade_fee_is_ceiled_to_a_millionth(self):
        from engine.fees import ceil_to, FEE_GRANULARITY
        assert ceil_to(D("0.0000001"), FEE_GRANULARITY) == D("0.000001")
        assert ceil_to(D("0.003638001"), FEE_GRANULARITY) == D("0.003639")

    def test_balance_change_is_floored_to_the_member_precision(self):
        from engine.fees import OrderFeeAccumulator, PRECISION_DIRECT
        direct = OrderFeeAccumulator(precision=PRECISION_DIRECT)
        r = direct.charge(revenue_usd=D("-0.055000"), model_fee_usd=D("0.00363825"))
        # A direct member is aligned to $0.0001, so far less is lost to
        # rounding than a non-direct member's $0.01.
        assert r.precision == PRECISION_DIRECT
        assert r.rounding_fee_usd < D("0.001361")

    def test_the_accumulator_rebates_overpaid_rounding_across_fills(self):
        """Without this, an order that fills in many small pieces is charged
        a fresh sub-cent round-up on each one and never refunded — which
        overstates the cost of exactly this strategy."""
        from engine.fees import OrderFeeAccumulator
        acc = OrderFeeAccumulator()
        rebates = D(0)
        for _ in range(40):
            r = acc.charge(revenue_usd=D("-0.055000"), model_fee_usd=D("0.00363825"))
            rebates += r.rebate_usd
        assert rebates > 0, "rounding overpayment was never rebated"

    def test_net_fee_is_never_negative(self):
        from engine.fees import OrderFeeAccumulator
        acc = OrderFeeAccumulator()
        acc.carried = D("5.00")          # a large carried credit
        r = acc.charge(revenue_usd=D("-0.50"), model_fee_usd=D("0.001"))
        assert r.net_fee_usd >= 0


class TestUnverifiedRateIsReportedAsARange:
    def test_range_brackets_the_working_assumption(self):
        from engine.fees import fee_range_usd, RATE_RANGE_HIGH
        lo, hi = fee_range_usd(50, 100)
        assert lo < hi
        assert hi == KALSHI_UNVERIFIED.fee_usd(50, 100)

    def test_range_low_end_is_a_bound_not_a_fact(self):
        """The low end comes from a search summary of the tier table, which
        may describe the perps schedule. It is a bound; it must not be
        promoted to the active schedule."""
        from engine.fees import RATE_RANGE_LOW
        assert KALSHI_UNVERIFIED.rate != RATE_RANGE_LOW
        assert KALSHI_UNVERIFIED.verified is False
