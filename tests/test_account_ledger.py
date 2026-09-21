"""P3 regressions — one account, real cash, real reservations (2026-09-21).

Before this, three inconsistent capital figures coexisted (BANKROLL_USD $80
default, a hardcoded $10,000 in QuoteManager._get_balance's paper branch, the
$5,000 actually intended) and nothing reserved capital at all: exposure was
inferred by summing resting orders, which answers "how much am I showing?"
not "can I afford this?". Two markets could each pass their own cap while
jointly exceeding the cash that exists.
"""
from __future__ import annotations

import sys
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine.account_ledger import (
    AccountLedger, InsufficientCapital, contract_cost_usd,
)
from execution.quote_manager import QuoteManager, QuoteTarget

TKR = "KXTEST-26SEP30-T1"
D = Decimal


@pytest.fixture
def acct():
    return AccountLedger(opening_cash_usd=5000, mode="paper")


def _reserve(a, oid, price, qty, market=TKR):
    return a.reserve(oid, market=market, program_id="p1",
                     price_cents=price, quantity=qty)


class TestCollateral:
    def test_cost_is_price_times_size(self):
        assert contract_cost_usd(50, 10) == D("5.00")
        assert contract_cost_usd(1, 1) == D("0.01")
        assert contract_cost_usd(99, 100) == D("99.00")

    def test_decimal_not_float(self):
        """0.1+0.2 arithmetic at settlement is a real bug, not pedantry."""
        total = sum((contract_cost_usd(10, 1) for _ in range(3)), D(0))
        assert total == D("0.30")
        assert isinstance(total, Decimal)


class TestReservations:
    def test_available_falls_but_cash_does_not(self, acct):
        _reserve(acct, "o1", 50, 10)
        s = acct.state()
        assert s.cash_usd == D(5000)          # nothing spent yet
        assert s.reserved_usd == D("5.00")
        assert s.available_usd == D("4995.00")

    def test_release_restores_available(self, acct):
        _reserve(acct, "o1", 50, 10)
        assert acct.release("o1") == D("5.00")
        assert acct.available_usd() == D(5000)

    def test_release_is_idempotent(self, acct):
        _reserve(acct, "o1", 50, 10)
        acct.release("o1")
        assert acct.release("o1") == D(0)
        assert acct.available_usd() == D(5000)

    def test_reserve_is_idempotent_by_order_id(self, acct):
        """A retried placement must not consume the account twice."""
        _reserve(acct, "o1", 50, 10)
        _reserve(acct, "o1", 50, 10)
        assert acct.state().reserved_usd == D("5.00")
        assert acct.state().n_reservations == 1

    def test_reserve_adjusts_an_existing_hold(self, acct):
        _reserve(acct, "o1", 50, 10)      # 5.00
        _reserve(acct, "o1", 50, 20)      # now 10.00, not 15.00
        assert acct.state().reserved_usd == D("10.00")

    def test_cannot_reserve_beyond_available(self, acct):
        _reserve(acct, "o1", 50, 9000)    # 4500
        with pytest.raises(InsufficientCapital) as e:
            _reserve(acct, "o2", 50, 1100)  # needs 550, only 500 left
        assert e.value.available == D("500.00")

    def test_the_joint_overspend_case(self, acct):
        """Each order is individually modest; together they exceed cash.
        Per-market gross caps never caught this."""
        a = AccountLedger(opening_cash_usd=100, mode="paper")
        for i in range(10):
            _reserve(a, f"o{i}", 50, 20, market=f"MKT-{i}")   # $10 each
        assert a.available_usd() == D(0)
        with pytest.raises(InsufficientCapital):
            _reserve(a, "o10", 50, 2, market="MKT-10")

    def test_can_afford_and_max_affordable(self, acct):
        assert acct.can_afford(50, 10000) is True       # exactly 5000
        assert acct.can_afford(50, 10001) is False
        assert acct.max_affordable_contracts(50) == 10000
        _reserve(acct, "o1", 50, 2000)                  # 1000 held
        assert acct.max_affordable_contracts(50) == 8000

    def test_max_affordable_handles_zero_price(self, acct):
        assert acct.max_affordable_contracts(0) == 0


class TestFillsAndSettlement:
    def test_fill_converts_reservation_to_inventory(self, acct):
        _reserve(acct, "o1", 50, 10)
        acct.on_fill("o1", market=TKR, program_id="p1", side="yes",
                     price_cents=50, quantity=10, trade_id="t1")
        s = acct.state()
        assert s.cash_usd == D("4995.00")        # cash actually left
        assert s.reserved_usd == D(0)            # hold consumed
        assert s.inventory_cost_usd == D("5.00")
        assert s.equity_usd == D("5000.00")      # nothing gained or lost yet

    def test_partial_fill_keeps_the_remaining_hold(self, acct):
        _reserve(acct, "o1", 50, 10)             # 5.00
        acct.on_fill("o1", market=TKR, program_id="p1", side="yes",
                     price_cents=50, quantity=4, trade_id="t1")
        s = acct.state()
        assert s.inventory_cost_usd == D("2.00")
        assert s.reserved_usd == D("3.00")       # 6 contracts still resting
        assert s.cash_usd == D("4998.00")

    def test_fee_reduces_cash_but_not_inventory_basis(self, acct):
        _reserve(acct, "o1", 50, 10)
        acct.on_fill("o1", market=TKR, program_id="p1", side="yes",
                     price_cents=50, quantity=10, fee_usd="0.25", trade_id="t1")
        s = acct.state()
        assert s.cash_usd == D("4994.75")
        assert s.inventory_cost_usd == D("5.00")
        assert s.equity_usd == D("4999.75")      # the fee is a real loss

    def test_winning_settlement_pays_one_dollar_per_contract(self, acct):
        _reserve(acct, "o1", 50, 10)
        acct.on_fill("o1", market=TKR, program_id="p1", side="yes",
                     price_cents=50, quantity=10, trade_id="t1")
        acct.on_settlement(market=TKR, program_id="p1", side="yes",
                           quantity=10, won=True, cost_basis_usd="5.00")
        s = acct.state()
        assert s.cash_usd == D("5005.00")        # paid 5, received 10
        assert s.inventory_cost_usd == D(0)

    def test_losing_settlement_pays_nothing(self, acct):
        _reserve(acct, "o1", 50, 10)
        acct.on_fill("o1", market=TKR, program_id="p1", side="yes",
                     price_cents=50, quantity=10, trade_id="t1")
        acct.on_settlement(market=TKR, program_id="p1", side="yes",
                           quantity=10, won=False, cost_basis_usd="5.00")
        s = acct.state()
        assert s.cash_usd == D("4995.00")        # the 5 is gone
        assert s.equity_usd == D("4995.00")


class TestRewardProvenanceConsistency:
    def test_estimated_reward_moves_no_cash(self, acct):
        acct.credit_reward("12.34", market=TKR, program_id="p1", paid=False)
        assert acct.state().cash_usd == D(5000)

    def test_paid_reward_moves_cash(self, acct):
        acct.credit_reward("12.34", market=TKR, program_id="p1", paid=True)
        assert acct.state().cash_usd == D("5012.34")


class TestConcurrency:
    def test_parallel_reservations_never_oversubscribe(self):
        """Fills arrive on the WS loop while reconcile runs in executors."""
        a = AccountLedger(opening_cash_usd=100, mode="paper")
        ok, refused = [], []
        barrier = threading.Barrier(20)

        def go(i):
            barrier.wait()
            try:
                _reserve(a, f"o{i}", 50, 20, market=f"M{i}")   # $10 each
                ok.append(i)
            except InsufficientCapital:
                refused.append(i)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert len(ok) == 10 and len(refused) == 10
        assert a.available_usd() == D(0)
        assert a.state().reserved_usd == D(100)


class TestEventTrail:
    def test_events_land_in_the_shared_store(self, tmp_path):
        """Paper runner and offline research must read one history."""
        db = str(tmp_path / "events.db")
        a = AccountLedger(opening_cash_usd=5000, mode="paper", event_db_path=db)
        a.reserve("o1", market=TKR, program_id="p1", price_cents=50,
                  quantity=10, ts_ms=1000)
        a.on_fill("o1", market=TKR, program_id="p1", side="yes",
                  price_cents=50, quantity=10, trade_id="t1", ts_ms=2000)
        from research.profit_ledger import ProfitLedger
        events = ProfitLedger(db).events("paper", 10_000)
        kinds = [e["kind"] for e in events]
        assert "reserve" in kinds and "buy" in kinds
        assert all(e["program_id"] == "p1" for e in events)

    def test_missing_event_store_does_not_break_accounting(self, tmp_path):
        a = AccountLedger(opening_cash_usd=100, mode="paper",
                          event_db_path=str(tmp_path / "nested" / "x.db"))
        _reserve(a, "o1", 50, 10)
        assert a.state().reserved_usd == D("5.00")


class TestQuoteManagerIntegration:
    def _qm(self, tmp_path, account, paper=True):
        qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"), account=account)
        qm.paper = paper
        qm._log_quote_row = MagicMock()
        qm._update_quote_status = MagicMock()
        return qm

    def test_placement_reserves_capital(self, tmp_path, acct):
        """Premium PLUS the maker fee allowance (2026-09-20 audit). A binary's
        collateral is exact, but the fee is not part of it: reserving premium
        alone leaves the account short at fill time and overstates what other
        markets may spend."""
        from engine.fees import fee_usd
        qm = self._qm(tmp_path, acct)
        r = qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49)
        assert r is not None
        expected = D("5.00") + fee_usd(50, 10, is_taker=False)
        assert acct.state().reserved_usd == expected
        assert expected >= D("5.00")

    def test_placement_refused_when_capital_exhausted(self, tmp_path):
        a = AccountLedger(opening_cash_usd=1, mode="paper")
        qm = self._qm(tmp_path, a)
        assert qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49) is None
        assert qm.capital_refusals == 1
        assert a.available_usd() == D(1)         # nothing held

    def test_cancel_releases_capital(self, tmp_path, acct):
        qm = self._qm(tmp_path, acct)
        from engine.fees import fee_usd
        r = qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49)
        assert acct.state().reserved_usd == D("5.00") + fee_usd(50, 10, is_taker=False)
        qm._cancel_order(r)
        assert acct.state().reserved_usd == D(0)
        assert acct.available_usd() == D(5000)

    def test_refused_maker_order_does_not_leak_a_reservation(self, tmp_path, acct):
        """A crossing quote is refused AFTER capital was held; the hold must
        not leak or the account bleeds capacity on every rejection."""
        qm = self._qm(tmp_path, acct)
        assert qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=50) is None
        assert acct.state().reserved_usd == D(0)
        assert acct.available_usd() == D(5000)

    def test_fill_spends_cash_through_the_account(self, tmp_path, acct):
        qm = self._qm(tmp_path, acct)
        r = qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49)
        qm.apply_fill(r.order_id, TKR, 10.0, trade_id="t1", side="yes",
                      price_cents=50)
        s = acct.state()
        assert s.cash_usd == D("4995.00") and s.inventory_cost_usd == D("5.00")

    def test_duplicate_fill_does_not_spend_twice(self, tmp_path, acct):
        qm = self._qm(tmp_path, acct)
        r = qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49)
        for _ in range(2):
            qm.apply_fill(r.order_id, TKR, 10.0, trade_id="t1", side="yes",
                          price_cents=50)
        assert qm.last_fill_status == "duplicate"
        assert acct.state().cash_usd == D("4995.00")

    def test_paper_balance_is_the_account_not_ten_thousand(self, tmp_path, acct):
        """The hardcoded $10,000 made MAX_BANKROLL_SHARE_PCT permit 100% of a
        $5,000 account."""
        qm = self._qm(tmp_path, acct)
        assert qm._get_balance() == 5000.0

    def test_paper_balance_without_account_uses_the_setting(self, tmp_path):
        qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
        assert qm._get_balance() == float(settings.ACCOUNT_OPENING_CASH_USD)
        assert qm._get_balance() != 10_000.0


class TestSettingsCoherence:
    def test_single_account_figure_is_five_thousand(self):
        assert settings.ACCOUNT_OPENING_CASH_USD == 5000.0
