"""P2 regressions — one maker-safe order contract (2026-09-21).

execution/quote_manager.py sent {"post_only": True}; venue/kalshi.py sent
{"no_self_trade": True} while asserting post_only did not exist. Those are
not equivalent: no_self_trade only blocks trading against your OWN resting
order and does nothing to stop a bid crossing a stranger's offer.

Because the live API cannot be verified from this environment (egress
blocked), maker safety is proven LOCALLY: on Kalshi a YES buy at p crosses
iff p + best_no_bid >= 100, since the two sides are mirror-priced.

venue/kalshi.py was archived 2026-10-01 (_archive/2026-10-01/venue/) with
its adapter tests; execution/quote_manager.py is now paper-only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.order_request import (
    CONTRACT_CENTS, MAKER_ONLY_FIELD, MakerSafetyError, assert_maker_safe,
    build_limit_order, would_cross,
)
from execution.quote_manager import QuoteManager, QuoteTarget

TKR = "KXTEST-26SEP30-T1"


@pytest.fixture
def allow_live(monkeypatch):
    """Lift the live-execution interlock for tests that exercise the code
    PAST it. The interlock itself is covered by TestLiveExecutionInterlock;
    without this fixture those tests would pass for the wrong reason."""
    import execution.order_request as orq
    monkeypatch.setattr(orq, "MAKER_ONLY_ENFORCEMENT_VERIFIED", True)
    # The quote manager and the Kalshi adapter pass venue="kalshi".
    monkeypatch.setattr(orq, "KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED", True)
    return True


class TestLiveExecutionInterlock:
    """A local non-crossing check is a preflight, not proof of maker
    execution: the book can move between our observation and arrival. Live
    transmission therefore stays blocked until exchange-enforced post_only
    is verified."""

    def test_enforcement_is_not_claimed_as_verified(self):
        import execution.order_request as orq
        # The FIELD is verified against the official schema...
        assert orq.MAKER_ONLY_FIELD_VERIFIED is True
        # ...but its enforcement semantics are not, and must not be
        # promoted to "verified" by assumption.
        assert orq.MAKER_ONLY_ENFORCEMENT_VERIFIED is False

    def test_gate_raises_while_enforcement_unverified(self):
        from execution.order_request import (
            LiveExecutionBlocked, require_live_execution_allowed)
        with pytest.raises(LiveExecutionBlocked) as e:
            require_live_execution_allowed()
        assert "PREFLIGHT" in str(e.value)

    def test_gate_passes_once_enforcement_is_verified(self, allow_live):
        from execution.order_request import require_live_execution_allowed
        require_live_execution_allowed()          # must not raise

    def test_live_placement_is_blocked_before_any_transmission(self, tmp_path):
        # Construct paper (the LIP_LIVE_ACK arming interlock forces it), then
        # flip to live exactly as the other live tests do.
        qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
        qm.paper = False
        qm.client = MagicMock()
        qm._log_quote_row = MagicMock()
        qm._passes_safety = lambda t: (True, "ok")
        # A perfectly passive, non-crossing order still does not go out.
        # 2026-10-01: the legacy QuoteManager now refuses live outright.
        with pytest.raises(RuntimeError, match="paper-only"):
            qm._place_order(TKR, "yes", 40, 10, best_opposing_bid_cents=50)
        qm.client.post.assert_not_called()

    def test_paper_is_unaffected_by_the_live_gate(self, tmp_path):
        qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
        qm._log_quote_row = MagicMock()
        assert qm._place_order(TKR, "yes", 40, 10,
                               best_opposing_bid_cents=50) is not None
        assert qm.live_blocked == 0


class TestObsoleteSelfTradeField:
    def test_no_self_trade_is_rejected_even_alongside_post_only(self):
        """It is not merely weaker than post_only — it is not in the current
        schema at all, so an adapter sending it has neither protection."""
        from execution.order_request import MakerSafetyError, assert_maker_safe
        body = {"action": "buy", "type": "limit", "post_only": True,
                "no_self_trade": True}
        with pytest.raises(MakerSafetyError) as e:
            assert_maker_safe(body)
        assert "self_trade_prevention_type" in str(e.value)


def _order(**kw):
    base = dict(ticker=TKR, side="yes", price_cents=49, size_contracts=10,
                client_order_id="LIP-abc", best_opposing_bid_cents=50)
    base.update(kw)
    return build_limit_order(**base)


class TestNonCrossing:
    def test_yes_buy_below_implied_offer_is_safe(self):
        # no_bid 50 ⇒ implied yes offer 50. A yes bid at 49 rests.
        chk = would_cross("yes", 49, best_opposing_bid_cents=50)
        assert chk and chk.implied_opposing_price == 50

    def test_yes_buy_at_implied_offer_crosses(self):
        chk = would_cross("yes", 50, best_opposing_bid_cents=50)
        assert not chk and "TAKE, not a make" in chk.reason

    def test_yes_buy_above_implied_offer_crosses(self):
        assert not would_cross("yes", 60, best_opposing_bid_cents=50)

    def test_sum_to_one_hundred_is_the_boundary(self):
        for yes, no in ((49, 50), (30, 69), (1, 98)):
            assert would_cross("yes", yes, best_opposing_bid_cents=no)
        for yes, no in ((50, 50), (31, 69), (2, 98)):
            assert not would_cross("yes", yes, best_opposing_bid_cents=no)

    def test_no_side_is_symmetric(self):
        assert would_cross("no", 49, best_opposing_bid_cents=50)
        assert not would_cross("no", 51, best_opposing_bid_cents=50)

    def test_unknown_opposing_book_is_unsafe(self):
        chk = would_cross("yes", 10, best_opposing_bid_cents=None)
        assert not chk and "cannot prove non-crossing" in chk.reason

    def test_invalid_side_rejected(self):
        assert not would_cross("maybe", 10, best_opposing_bid_cents=50)


class TestBuildLimitOrder:
    def test_carries_maker_flag_and_correct_price_field(self):
        b = _order()
        assert b[MAKER_ONLY_FIELD] is True
        assert b["yes_price"] == 49 and "no_price" not in b
        assert b["action"] == "buy" and b["type"] == "limit"
        assert b["count"] == 10 and b["client_order_id"] == "LIP-abc"

    def test_no_side_uses_no_price(self):
        b = _order(side="no", price_cents=49, best_opposing_bid_cents=50)
        assert b["no_price"] == 49 and "yes_price" not in b

    def test_never_emits_no_self_trade_as_a_substitute(self):
        """The specific defect: no_self_trade standing in for maker protection."""
        assert "no_self_trade" not in _order()

    def test_crossing_order_is_refused(self):
        with pytest.raises(MakerSafetyError, match="TAKE, not a make"):
            _order(price_cents=50, best_opposing_bid_cents=50)

    def test_unknown_opposing_book_is_refused(self):
        with pytest.raises(MakerSafetyError, match="cannot prove non-crossing"):
            _order(best_opposing_bid_cents=None)

    def test_deliberate_crossing_must_be_explicit(self):
        b = _order(price_cents=60, best_opposing_bid_cents=50,
                   enforce_non_crossing=False)
        assert b["yes_price"] == 60      # allowed, but only by saying so

    @pytest.mark.parametrize("price", [0, CONTRACT_CENTS, -1, 101])
    def test_edge_prices_rejected(self, price):
        with pytest.raises(MakerSafetyError, match="outside"):
            _order(price_cents=price, best_opposing_bid_cents=0)

    def test_non_positive_size_rejected(self):
        with pytest.raises(MakerSafetyError, match="non-positive size"):
            _order(size_contracts=0)

    def test_missing_client_order_id_rejected(self):
        with pytest.raises(MakerSafetyError, match="client_order_id"):
            _order(client_order_id="")


class TestAssertMakerSafe:
    def test_accepts_a_built_order(self):
        assert_maker_safe(_order())

    def test_rejects_missing_maker_flag(self):
        b = _order(); b.pop(MAKER_ONLY_FIELD)
        with pytest.raises(MakerSafetyError, match="missing"):
            assert_maker_safe(b)

    def test_rejects_no_self_trade_substitution_with_an_explanation(self):
        b = _order(); b.pop(MAKER_ONLY_FIELD); b["no_self_trade"] = True
        with pytest.raises(MakerSafetyError, match="only blocks trading against your OWN"):
            assert_maker_safe(b)

    def test_rejects_non_limit_or_sell(self):
        b = _order(); b["type"] = "market"
        with pytest.raises(MakerSafetyError, match="must be limit"):
            assert_maker_safe(b)
        b = _order(); b["action"] = "sell"
        with pytest.raises(MakerSafetyError, match="passive buys"):
            assert_maker_safe(b)


class TestQuoteManagerUsesSharedContract:
    def _qm(self, tmp_path, paper: bool):
        qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
        qm.paper = paper
        qm.client = MagicMock()
        qm.client.post.return_value = {"order": {"order_id": "srv-1"}}
        qm._log_quote_row = MagicMock()
        return qm

    # 2026-10-01: the three live wire-body tests that stood here
    # (builder body, crossing refusal, unknown-book refusal) exercised the
    # legacy live branch, which now raises before any body is built.
    def test_live_placement_raises_even_with_gates_lifted(self, tmp_path,
                                                          allow_live):
        qm = self._qm(tmp_path, paper=False)
        with pytest.raises(RuntimeError, match="paper-only"):
            qm._place_order(TKR, "yes", 49, 10, best_opposing_bid_cents=50)
        qm.client.post.assert_not_called()

    def test_paper_refuses_crossing_quote_too(self, tmp_path):
        """Catch a crossing quote in paper rather than discovering it live."""
        qm = self._qm(tmp_path, paper=True)
        assert qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=50) is None

    def test_paper_still_runs_without_an_opposing_book(self, tmp_path):
        qm = self._qm(tmp_path, paper=True)
        assert qm._place_order(TKR, "yes", 49, 10) is not None

    def test_reconcile_passes_the_other_leg_as_opposing_bid(self, tmp_path):
        """A two-sided target supplies its own crossing check."""
        qm = self._qm(tmp_path, paper=True)
        seen = {}

        def spy(ticker, side, price, size, best_opposing_bid_cents=None,
                program_id=""):
            seen[side] = best_opposing_bid_cents
            return None
        qm._place_order = spy
        qm._passes_safety = lambda t: (True, "ok")
        qm.reconcile(QuoteTarget(market_ticker=TKR, yes_bid_cents=49,
                                 no_bid_cents=48, size_contracts=10))
        assert seen == {"yes": 48, "no": 49}
