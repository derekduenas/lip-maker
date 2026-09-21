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
        # 0.07 x 10 x 0.5 x 0.5 = 0.175 dollars = 17.5c -> ceil 18c
        assert fee_usd(50, 10) == D("0.18")

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


class TestRoundUp:
    def test_round_up_is_applied(self):
        """The defect: documented as ceil, implemented nowhere."""
        # 1 contract @ 50c: raw 1.75c -> 2c
        assert fee_usd(50, 1) == D("0.02")

    def test_round_up_dominates_small_fills(self):
        """1 contract at 5c costs 0.3325c raw; the ceiling makes it 1c —
        a ~3x understatement if you skip it, and far worse in aggregate."""
        raw = KALSHI_UNVERIFIED.rate * 1 * D("0.05") * D("0.95") * 100
        assert raw < D("0.34")
        assert fee_usd(5, 1) == D("0.01")

    def test_unrounded_schedule_differs(self):
        unrounded = FeeSchedule(name="t", rate=D("0.07"), source="test",
                                round_up_to_cent=False)
        assert unrounded.fee_usd(50, 1) == D("0.0175")
        assert KALSHI_UNVERIFIED.fee_usd(50, 1) == D("0.02")

    def test_per_fill_rounding_beats_aggregate_rounding(self):
        """Ten 1-contract fills cost more than one 10-contract fill. Any
        code that sums contracts first and rounds once understates."""
        ten_singles = sum((fee_usd(50, 1) for _ in range(10)), D(0))
        one_block = fee_usd(50, 10)
        assert ten_singles == D("0.20") and one_block == D("0.18")
        assert ten_singles > one_block


class TestProvenance:
    def test_default_schedule_is_not_verified(self):
        assert KALSHI_UNVERIFIED.verified is False
        assert "egress blocked" in KALSHI_UNVERIFIED.source

    def test_require_verified_refuses_to_guess(self):
        with pytest.raises(UnverifiedFeeSchedule, match="unverified"):
            fee_usd(50, 10, require_verified=True)
        with pytest.raises(UnverifiedFeeSchedule):
            round_trip_usd(50, 50, 10, require_verified=True)

    def test_verified_schedule_satisfies_the_requirement(self):
        fees.set_schedule(FeeSchedule(name="confirmed", rate=D("0.07"),
                                      source="operator confirmed", verified=True,
                                      verified_at="2026-09-21"))
        assert fee_usd(50, 10, require_verified=True) == D("0.18")
        assert provenance_warning() is None

    def test_warning_names_the_schedule(self):
        w = provenance_warning()
        assert "UNVERIFIED" in w and "kalshi_documented_unverified" in w

    def test_describe_carries_the_audit_fields(self):
        d = KALSHI_UNVERIFIED.describe()
        assert d["verified"] is False and d["round_up_to_cent"] is True
        assert d["charge_maker"] is True and d["source"]


class TestMakerAssumption:
    def test_default_is_conservative_and_charges_makers(self):
        """The replaced assumption was fee-free makers, which inflates net
        yield. Default now assumes we pay."""
        assert KALSHI_UNVERIFIED.charge_maker is True
        assert fee_usd(50, 10, is_taker=False) > 0

    def test_legacy_free_maker_schedule_available_for_ab(self):
        fees.set_schedule(ASSUME_FREE_MAKER)
        assert fee_usd(50, 10, is_taker=False) == D("0")
        assert fee_usd(50, 10, is_taker=True) == D("0.18")   # takers still pay

    def test_round_trip_charges_entry_and_exit(self):
        rt = round_trip_usd(50, 50, 10)
        assert rt == D("0.36")      # maker in, taker out


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

    def test_each_fill_is_rounded_separately(self, tmp_path):
        from tools.net_yield_logger import _day_fees_usd
        conn = self._db(tmp_path, [("yes", 1, 50, 50, 0)] * 10)
        assert _day_fees_usd(conn, "2026-09-20") == D("0.20")   # not 0.18

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
