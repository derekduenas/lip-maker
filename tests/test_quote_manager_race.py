"""C2: cancel-then-place race must not produce double same-side orders.

When _cancel_order fails (returns False), placement should be SKIPPED and
the resting order should be marked pending_cancel for next reconcile.
"""
import time
import pytest
from unittest.mock import patch


def _fresh_qm(monkeypatch):
    """Fresh paper-mode QuoteManager with a dummy resting yes order."""
    from execution.quote_manager import QuoteManager, RestingOrder, QuoteTarget
    qm = QuoteManager(paper=True)
    qm.resting["TEST-MKT"] = [
        RestingOrder(
            order_id="OLD-YES-1", market_ticker="TEST-MKT",
            side="yes", price_cents=40, size_contracts=25,
            placed_at=time.time(), paper=True,
        )
    ]
    target = QuoteTarget(
        market_ticker="TEST-MKT",
        yes_bid_cents=42,         # different price → triggers replace
        no_bid_cents=58,
        size_contracts=25,
    )
    return qm, target


def test_cancel_failure_skips_placement_and_flags_pending(monkeypatch):
    from execution.quote_manager import QuoteManager
    qm, target = _fresh_qm(monkeypatch)

    # Simulate _cancel_order returning False (network err / silent fail)
    def fail_cancel(order):
        return False
    monkeypatch.setattr(qm, "_cancel_order", fail_cancel)

    place_calls = []

    def fake_place(ticker, side, price, size):
        place_calls.append((ticker, side, price, size))
        return None
    monkeypatch.setattr(qm, "_place_order", fake_place)
    # Stub safety + sizing to avoid touching the network
    monkeypatch.setattr(qm, "_passes_safety", lambda t: (True, "ok"))
    monkeypatch.setattr(qm, "_refresh_inventory", lambda mkt: None)
    qm.inventory = {}

    actions = qm.reconcile(target)

    # Key invariants
    assert qm.resting["TEST-MKT"][0].pending_cancel is True, \
        "failed cancel must flag pending_cancel"
    yes_places = [c for c in place_calls if c[1] == "yes"]
    assert len(yes_places) == 0, \
        "placement on yes side must be skipped when cancel failed"
    assert "pending_cancel_yes" in actions
    assert actions["pending_cancel_yes"] == 1


def test_cancel_success_does_place(monkeypatch):
    """Sanity: when cancel succeeds, we DO place the new order."""
    from execution.quote_manager import QuoteManager
    qm, target = _fresh_qm(monkeypatch)

    monkeypatch.setattr(qm, "_cancel_order", lambda o: True)
    place_calls = []
    monkeypatch.setattr(qm, "_place_order",
                        lambda t, s, p, sz: place_calls.append((t, s, p, sz)))
    monkeypatch.setattr(qm, "_passes_safety", lambda t: (True, "ok"))
    monkeypatch.setattr(qm, "_refresh_inventory", lambda mkt: None)
    qm.inventory = {}

    qm.reconcile(target)

    yes_places = [c for c in place_calls if c[1] == "yes"]
    assert len(yes_places) == 1
    assert yes_places[0][2] == 42  # new price
