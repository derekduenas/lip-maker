"""Shared-account audit along the real runner call paths (2026-09-20).

Two defects this pins, both found by tracing placement/cancel rather than
reading unit tests:

1. An exception from the order POST released the reservation. An exception
   does NOT prove the order failed to reach the exchange — a timeout or
   reset can occur after acceptance. Releasing there treats an UNKNOWN
   outcome as an authoritative rejection and lets the account spend the
   same dollars twice.

2. Reconciliation tombstoned a phantom order but never released its
   capital, so the reservation outlived the order and the shared account
   starved one leak at a time.

Capital is released only on an authoritative outcome: a venue view that
proves the order does not exist, or an explicit cancel.
"""
from __future__ import annotations

import sys
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.account_ledger import AccountLedger
from execution.quote_manager import QuoteManager, RestingOrder

TKR = "KXAUDIT-26SEP30-T1"


@pytest.fixture
def acct():
    return AccountLedger(opening_cash_usd=5000, mode="paper")


@pytest.fixture
def allow_live(monkeypatch):
    import execution.order_request as orq
    monkeypatch.setattr(orq, "MAKER_ONLY_ENFORCEMENT_VERIFIED", True)


def _live_qm(tmp_path, acct):
    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"), account=acct)
    qm.paper = False
    qm.client = MagicMock()
    qm._log_quote_row = MagicMock()
    qm._update_quote_status = MagicMock()
    return qm


# ── 1. unknown submission outcome ─────────────────────────────────────────

def test_unknown_submission_keeps_the_reservation(tmp_path, acct, allow_live):
    qm = _live_qm(tmp_path, acct)
    qm.client.post.side_effect = TimeoutError("read timeout after send")
    assert qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49) is None
    # The order may well be resting on the venue. The money stays held.
    assert acct.state().reserved_usd > D(0), \
        "an unknown outcome released capital — the account can now double-spend"
    assert qm.submit_unknown == 1
    assert TKR in qm.uncertain_markets


def test_unknown_submission_is_recorded_for_reconciliation(tmp_path, acct, allow_live):
    qm = _live_qm(tmp_path, acct)
    qm.client.post.side_effect = ConnectionResetError("reset")
    qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49)
    assert len(qm._unknown_submissions) == 1


def test_deterministic_refusal_still_releases_immediately(tmp_path, acct, allow_live):
    """A crossing order never leaves the process, so holding its capital
    would be a leak, not caution."""
    qm = _live_qm(tmp_path, acct)
    assert qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=50) is None
    assert acct.state().reserved_usd == D(0)
    qm.client.post.assert_not_called()


# ── 2. reconciliation is the authoritative release ────────────────────────

def test_reconciliation_releases_an_unknown_submission_the_venue_never_saw(
        tmp_path, acct, allow_live):
    qm = _live_qm(tmp_path, acct)
    qm.client.post.side_effect = TimeoutError("timeout")
    qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49)
    held = acct.state().reserved_usd
    assert held > D(0)
    # Venue view: no such order. That is authoritative.
    qm._merge_live_orders({})
    assert acct.state().reserved_usd == D(0), \
        "reconciliation did not free capital for an order that never existed"
    assert qm._unknown_submissions == {}


def test_reconciliation_retains_capital_when_the_order_does_exist(
        tmp_path, acct, allow_live):
    qm = _live_qm(tmp_path, acct)
    qm.client.post.side_effect = TimeoutError("timeout")
    qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49)
    coid = next(iter(qm._unknown_submissions))
    held = acct.state().reserved_usd
    live = {"venue-1": RestingOrder(
        order_id="venue-1", market_ticker=TKR, side="yes", price_cents=50,
        size_contracts=10.0, placed_at=0.0, paper=False, client_order_id=coid)}
    qm._merge_live_orders(live)
    assert acct.state().reserved_usd == held, \
        "capital was released for an order that IS resting on the venue"
    assert qm._unknown_submissions == {}


def test_phantom_order_releases_its_capital(tmp_path, acct, allow_live):
    """A local order absent from the venue view is gone; its hold must go
    with it or the shared account starves."""
    qm = _live_qm(tmp_path, acct)
    qm.client.post.return_value = {"order": {"order_id": "venue-9"}}
    r = qm._place_order(TKR, "yes", 50, 10, best_opposing_bid_cents=49)
    assert r is not None and acct.state().reserved_usd > D(0)
    qm._merge_live_orders({})           # venue says: no orders
    assert acct.state().reserved_usd == D(0)


# ── 3. one shared account across markets ──────────────────────────────────

def test_all_markets_compete_for_the_same_account(tmp_path, acct, allow_live):
    qm = _live_qm(tmp_path, acct)
    qm.client.post.return_value = {"order": {"order_id": "v"}}
    before = acct.available_usd()
    qm._place_order("KXA-1", "yes", 50, 10, best_opposing_bid_cents=49)
    qm._place_order("KXB-2", "yes", 50, 10, best_opposing_bid_cents=49)
    spent = before - acct.available_usd()
    assert spent > D("10.00"), "two markets did not draw on one balance"
