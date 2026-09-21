"""Causal paper fills: we fill only when an observed trade reached us."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.kalshi_ws import BookLevel, BookState
from execution.paper_fills import PaperFillSimulator

TKR = "KXFILL-26SEP30-T1"


def _book(yes=((45, 100.0),), no=((50, 100.0),)):
    b = BookState(market_ticker=TKR)
    b.yes_bids = [BookLevel(p, s) for p, s in yes]
    b.no_bids = [BookLevel(p, s) for p, s in no]
    b.snapshot_count = 1
    return b


def _trade(tid, *, yes_c=45, no_c=55, taker="no", qty=10.0, when="2030-01-01T00:00:00Z"):
    return {"trade_id": tid, "ticker": TKR, "count_fp": str(qty),
            "yes_price_dollars": f"{yes_c/100:.4f}",
            "no_price_dollars": f"{no_c/100:.4f}",
            "taker_side": taker, "created_time": when}


def _sim(**kw):
    return PaperFillSimulator(latency_ms=0.0, **kw)


def _track(sim, *, price=45, size=10.0, side="yes", queue=0.0):
    book = _book(yes=((price, queue),)) if side == "yes" else _book(no=((price, queue),))
    return sim.track(order_id="o1", market_ticker=TKR, side=side,
                     price_cents=price, size=size, book=book, now=0.0)


# ── queue ─────────────────────────────────────────────────────────────────

def test_we_join_the_back_of_the_queue():
    sim = _sim()
    o = _track(sim, queue=250.0)
    assert o.queue_ahead == 250.0, "we assumed priority we did not earn"


def test_queue_ahead_must_be_consumed_before_we_fill():
    sim = _sim()
    _track(sim, size=10.0, queue=100.0)
    fills = sim.apply_trades([_trade("t1", qty=60.0)])
    assert fills == [], "filled while 100 contracts were still ahead of us"
    assert sim.orders["o1"].queue_ahead == 40.0


def test_we_fill_once_the_queue_clears():
    sim = _sim()
    _track(sim, size=10.0, queue=100.0)
    sim.apply_trades([_trade("t1", qty=100.0)])
    fills = sim.apply_trades([_trade("t2", qty=10.0)])
    assert len(fills) == 1 and fills[0]["count"] == 10.0


def test_a_fill_never_exceeds_remaining_size():
    sim = _sim()
    _track(sim, size=10.0, queue=0.0)
    fills = sim.apply_trades([_trade("t1", qty=1_000.0)])
    assert fills[0]["count"] == 10.0


# ── causality ─────────────────────────────────────────────────────────────

def test_a_trade_before_activation_is_not_ours():
    sim = PaperFillSimulator(latency_ms=5_000.0)
    book = _book(yes=((45, 0.0),))
    sim.track(order_id="o1", market_ticker=TKR, side="yes", price_cents=45,
              size=10.0, book=book, now=time.time())
    # trade timestamped in 1970 — long before our order existed
    assert sim.apply_trades([_trade("t1", when="1970-01-01T00:00:00Z")]) == []


def test_a_trade_at_another_price_does_not_fill_us():
    """A sweep that stops one cent away is not a fill."""
    sim = _sim()
    _track(sim, price=45, queue=0.0)
    assert sim.apply_trades([_trade("t1", yes_c=46)]) == []


def test_a_taker_on_our_own_side_does_not_fill_us():
    """Our YES bid is hit by a taker BUYING NO. A taker buying YES lifts
    offers; it does not trade against our bid."""
    sim = _sim()
    _track(sim, price=45, queue=0.0)
    assert sim.apply_trades([_trade("t1", taker="yes")]) == []


def test_no_side_order_is_filled_by_a_yes_taker():
    sim = _sim()
    _track(sim, side="no", price=50, queue=0.0)
    fills = sim.apply_trades([_trade("t1", no_c=50, taker="yes")])
    assert len(fills) == 1 and fills[0]["side"] == "no"


def test_each_trade_is_consumed_only_once():
    sim = _sim()
    _track(sim, size=100.0, queue=0.0)
    t = _trade("t1", qty=10.0)
    sim.apply_trades([t])
    assert sim.apply_trades([t]) == [], "the same trade filled us twice"


def test_every_fill_names_the_trade_that_caused_it():
    sim = _sim()
    _track(sim, queue=0.0)
    fills = sim.apply_trades([_trade("t-abc")])
    assert fills[0]["trade_id"] == "t-abc"


def test_untracked_orders_stop_filling():
    sim = _sim()
    _track(sim, queue=0.0)
    sim.untrack("o1")
    assert sim.apply_trades([_trade("t1")]) == []
