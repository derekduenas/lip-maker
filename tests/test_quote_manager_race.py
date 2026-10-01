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
    """Two resting orders on the side still cancel-replace. A failed cancel
    must not be followed by a new order."""
    from execution.quote_manager import QuoteManager, RestingOrder
    qm, target = _fresh_qm(monkeypatch)
    qm.resting["TEST-MKT"].append(RestingOrder(
        order_id="OLD-YES-2", market_ticker="TEST-MKT", side="yes",
        price_cents=40, size_contracts=25, placed_at=time.time(), paper=True,
    ))

    def fail_cancel(order):
        return False
    # Duplicate-side cleanup would collapse the two YES orders before the
    # cancel-replace path, and it writes the quotes table. This test is the
    # many-order path, so leave both orders in place.
    monkeypatch.setattr(qm, "_sanity_resting", lambda m: None)
    monkeypatch.setattr(qm, "_cancel_order", fail_cancel)

    place_calls = []

    def fake_place(ticker, side, price, size, best_opposing_bid_cents=None,
                   program_id=""):
        place_calls.append((ticker, side, price, size))
        return None
    monkeypatch.setattr(qm, "_place_order", fake_place)
    monkeypatch.setattr(qm, "_passes_safety", lambda t: (True, "ok"))
    monkeypatch.setattr(qm, "_refresh_inventory", lambda mkt: None)
    qm.inventory = {}

    actions = qm.reconcile(target)

    assert qm.resting["TEST-MKT"][0].pending_cancel is True
    yes_places = [c for c in place_calls if c[1] == "yes"]
    assert len(yes_places) == 0
    assert actions["pending_cancel_yes"] == 2


def test_price_change_amends_in_place_and_does_not_restack(monkeypatch):
    """One order, new price: amend. The order id stays. No second place."""
    from execution.quote_manager import RestingOrder
    qm, target = _fresh_qm(monkeypatch)
    qm.resting["TEST-MKT"].append(RestingOrder(
        order_id="OLD-NO-1", market_ticker="TEST-MKT", side="no",
        price_cents=58, size_contracts=25, placed_at=time.time(), paper=True,
    ))
    monkeypatch.setattr(qm, "_passes_safety", lambda t: (True, "ok"))
    monkeypatch.setattr(qm, "_refresh_inventory", lambda mkt: None)
    qm.inventory = {}
    place_calls = []
    monkeypatch.setattr(qm, "_place_order",
                        lambda t, s, p, sz, best_opposing_bid_cents=None, program_id="":
                            place_calls.append(1))
    qm.reconcile(target)
    yes = [o for o in qm.resting["TEST-MKT"] if o.side == "yes"]
    assert len(yes) == 1 and yes[0].order_id == "OLD-YES-1"
    assert yes[0].price_cents == 42 and yes[0].queue_preserved is False
    assert place_calls == []


def test_size_down_decreases_and_keeps_queue(monkeypatch):
    from execution.quote_manager import QuoteTarget, RestingOrder
    qm, _target = _fresh_qm(monkeypatch)
    qm.resting["TEST-MKT"][0].price_cents = 42
    qm.resting["TEST-MKT"].append(RestingOrder(
        order_id="OLD-NO-1", market_ticker="TEST-MKT", side="no",
        price_cents=58, size_contracts=10, placed_at=time.time(), paper=True,
    ))
    target = QuoteTarget(market_ticker="TEST-MKT", yes_bid_cents=42,
                         no_bid_cents=58, size_contracts=10)
    monkeypatch.setattr(qm, "_passes_safety", lambda t: (True, "ok"))
    monkeypatch.setattr(qm, "_refresh_inventory", lambda mkt: None)
    qm.inventory = {}
    qm.reconcile(target)
    yes = [o for o in qm.resting["TEST-MKT"] if o.side == "yes"]
    assert yes[0].size_contracts == 10 and yes[0].queue_preserved is True
    assert yes[0].order_id == "OLD-YES-1"


def test_amend_failure_does_not_place(monkeypatch):
    from execution.quote_manager import RestingOrder
    qm, target = _fresh_qm(monkeypatch)
    qm.resting["TEST-MKT"].append(RestingOrder(
        order_id="OLD-NO-1", market_ticker="TEST-MKT", side="no",
        price_cents=58, size_contracts=25, placed_at=time.time(), paper=True,
    ))
    monkeypatch.setattr(qm, "_amend_order", lambda *a, **k: False)
    monkeypatch.setattr(qm, "_passes_safety", lambda t: (True, "ok"))
    monkeypatch.setattr(qm, "_refresh_inventory", lambda mkt: None)
    qm.inventory = {}
    place_calls = []
    monkeypatch.setattr(qm, "_place_order",
                        lambda t, s, p, sz, best_opposing_bid_cents=None, program_id="":
                            place_calls.append((t, s, p, sz)))
    actions = qm.reconcile(target)
    assert place_calls == []
    assert actions["pending_cancel_yes"] == 1


def test_cancel_success_does_place(monkeypatch):
    """Two orders on one side still cancel then place. One order amends."""
    from execution.quote_manager import RestingOrder
    qm, target = _fresh_qm(monkeypatch)
    qm.resting["TEST-MKT"].append(RestingOrder(
        order_id="OLD-YES-2", market_ticker="TEST-MKT", side="yes",
        price_cents=40, size_contracts=25, placed_at=time.time(), paper=True,
    ))

    monkeypatch.setattr(qm, "_sanity_resting", lambda m: None)
    monkeypatch.setattr(qm, "_cancel_order", lambda o: True)
    place_calls = []
    monkeypatch.setattr(qm, "_place_order",
                        lambda t, s, p, sz, best_opposing_bid_cents=None, program_id="":
                            place_calls.append((t, s, p, sz)))
    monkeypatch.setattr(qm, "_passes_safety", lambda t: (True, "ok"))
    monkeypatch.setattr(qm, "_refresh_inventory", lambda mkt: None)
    qm.inventory = {}

    qm.reconcile(target)

    yes_places = [c for c in place_calls if c[1] == "yes"]
    assert len(yes_places) == 1
    assert yes_places[0][2] == 42  # new price
