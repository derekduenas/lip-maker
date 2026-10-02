"""Review (2026-10-01): the legacy QuoteManager is paper-only.

mm.unattended is the only code path allowed to reach a live venue. The
legacy execution/quote_manager.py live branch (place / decrease / amend /
cancel) must refuse outright, even when every other live gate is lifted
and a client object is present. Reads (resync) stay available.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from execution.quote_manager import QuoteManager, RestingOrder

TKR = "KXTEST-26SEP30-T1"
MSG = "legacy QuoteManager is paper-only; use mm.unattended"


@pytest.fixture
def live_qm(tmp_path, monkeypatch):
    # Lift every other gate so the refusal is the only thing that can stop it.
    import execution.order_request as orq
    monkeypatch.setattr(orq, "MAKER_ONLY_ENFORCEMENT_VERIFIED", True)
    monkeypatch.setattr(orq, "KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED", True)
    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
    qm.paper = False
    qm.client = MagicMock()
    qm.client.post.return_value = {"order": {"order_id": "srv-1"}}
    qm._log_quote_row = MagicMock()
    qm._update_quote_status = MagicMock()
    return qm


def _resting(qm):
    o = RestingOrder(order_id="srv-1", market_ticker=TKR, side="yes",
                     price_cents=49, size_contracts=10.0, placed_at=0.0,
                     paper=False, client_order_id="LIP-x")
    qm.resting[TKR] = [o]
    return o


def test_live_place_raises(live_qm):
    with pytest.raises(RuntimeError, match=MSG):
        live_qm._place_order(TKR, "yes", 49, 10, best_opposing_bid_cents=50)
    live_qm.client.post.assert_not_called()


def test_live_decrease_raises(live_qm):
    o = _resting(live_qm)
    with pytest.raises(RuntimeError, match=MSG):
        live_qm._decrease_order(o, 5)
    live_qm.client.post.assert_not_called()
    assert o.size_contracts == 10.0


def test_live_amend_raises(live_qm):
    o = _resting(live_qm)
    with pytest.raises(RuntimeError, match=MSG):
        live_qm._amend_order(o, 48, 10, best_opposing_bid_cents=50)
    live_qm.client.post.assert_not_called()
    assert o.price_cents == 49


def test_live_cancel_raises(live_qm):
    o = _resting(live_qm)
    with pytest.raises(RuntimeError, match=MSG):
        live_qm._cancel_order(o)
    live_qm.client.delete.assert_not_called()
    assert live_qm.resting[TKR] == [o]


def test_paper_paths_still_work(tmp_path):
    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
    qm._log_quote_row = MagicMock()
    qm._update_quote_status = MagicMock()
    r = qm._place_order(TKR, "yes", 49, 10, best_opposing_bid_cents=50)
    assert r is not None
    assert qm._cancel_order(r) is True
