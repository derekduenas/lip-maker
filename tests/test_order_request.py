"""P2 regressions — one maker-safe order contract (2026-09-21).

execution/quote_manager.py sent {"post_only": True}; venue/kalshi.py sent
{"no_self_trade": True} while asserting post_only did not exist. Those are
not equivalent: no_self_trade only blocks trading against your OWN resting
order and does nothing to stop a bid crossing a stranger's offer.

Because the live API cannot be verified from this environment (egress
blocked), maker safety is proven LOCALLY: on Kalshi a YES buy at p crosses
iff p + best_no_bid >= 100, since the two sides are mirror-priced.
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

    def test_live_order_body_comes_from_the_builder(self, tmp_path):
        qm = self._qm(tmp_path, paper=False)
        qm._update_quote_status = MagicMock()
        r = qm._place_order(TKR, "yes", 49, 10, best_opposing_bid_cents=50)
        assert r is not None
        body = qm.client.post.call_args[0][1]
        assert body[MAKER_ONLY_FIELD] is True and "no_self_trade" not in body
        assert body["yes_price"] == 49

    def test_live_crossing_order_is_refused_before_transmission(self, tmp_path):
        qm = self._qm(tmp_path, paper=False)
        assert qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=50) is None
        qm.client.post.assert_not_called()
        note = qm._log_quote_row.call_args.kwargs["notes"]
        assert "maker_safety" in note

    def test_live_order_without_opposing_book_is_refused(self, tmp_path):
        qm = self._qm(tmp_path, paper=False)
        assert qm._place_order(TKR, "yes", 49, 10) is None
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


class TestVenueAdapterConsolidated:
    def test_venue_adapter_no_longer_substitutes_no_self_trade(self, monkeypatch):
        from venue.kalshi import KalshiVenue
        v = KalshiVenue.__new__(KalshiVenue)      # skip auth in __init__
        client = MagicMock()
        client.post.return_value = {"order": {"order_id": "srv-9"}}
        v._client = client
        res = v.place_order(TKR, "yes", 49, 10, best_opposing_bid_cents=50)
        assert res.success
        body = client.post.call_args[0][1]
        assert body[MAKER_ONLY_FIELD] is True
        assert "no_self_trade" not in body

    def test_venue_adapter_refuses_a_crossing_order(self):
        from venue.kalshi import KalshiVenue
        v = KalshiVenue.__new__(KalshiVenue)
        v._client = MagicMock()
        res = v.place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=50)
        assert not res.success and "maker safety" in res.error
        v._client.post.assert_not_called()

    def test_venue_adapter_refuses_without_opposing_book(self):
        from venue.kalshi import KalshiVenue
        v = KalshiVenue.__new__(KalshiVenue)
        v._client = MagicMock()
        res = v.place_order(TKR, "yes", 49, 10)
        assert not res.success
        v._client.post.assert_not_called()
