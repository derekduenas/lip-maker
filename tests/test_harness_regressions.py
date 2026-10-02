"""Regressions from the overnight paper harness on commit 622c4e7."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from config import constitution, settings
from execution.kalshi_ws import BookLevel, BookState, FillEvent
from execution.paper_fills import PaperFillSimulator
from execution.quote_manager import QuoteManager, QuoteTarget, RestingOrder
from init_db import init_db
from mm.diff import plan_resting
from mm.risk import FILL_CLOCK
from risk.sentinel import Sentinel


TKR = "KXTEST-HARNESS"


def _db(tmp_path):
    path = str(tmp_path / "harness.db")
    init_db(path)
    return path


def _target(**kw):
    base = dict(market_ticker=TKR, yes_bid_cents=45, no_bid_cents=50, size_contracts=25)
    base.update(kw)
    return QuoteTarget(**base)


@pytest.fixture
def quiet_sentinel(monkeypatch):
    Sentinel.reset_rate_clock()
    FILL_CLOCK.reset()
    monkeypatch.setattr(constitution, "PAPER_BYPASS", False)
    monkeypatch.setattr("engine.series_ev.check_series_ev", lambda *a, **k: (True, ""))
    yield
    Sentinel.reset_rate_clock()
    FILL_CLOCK.reset()


def test_noop_reconcile_does_not_count_as_a_quote(tmp_path, quiet_sentinel):
    qm = QuoteManager(paper=True, db_path=_db(tmp_path))
    first = qm.reconcile(_target())
    assert first.get("placed") == 2
    writes = len(Sentinel._quote_timestamps)
    assert writes == 2
    for _ in range(30):
        again = qm.reconcile(_target())
        assert again.get("kept") == 2
    assert len(Sentinel._quote_timestamps) == writes


def test_one_cent_move_does_not_amend():
    assert plan_resting(40, 25, 41, 25).action == "keep"
    assert plan_resting(40, 25, 40, 26).action == "keep"
    assert plan_resting(40, 25, 42, 25).action == "amend"


def test_rate_limit_leaves_resting_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", _db(tmp_path))
    from run_paper import PaperRunner
    runner = PaperRunner([{
        "id": "p1", "market_ticker": TKR, "target_size": 100,
        "discount_factor": 0.5, "reward_per_day_usd": 10,
        "period_reward_usd": 10, "period_seconds": 86400,
        "start_date": "2026-01-01T00:00:00Z", "end_date": "2028-01-01T00:00:00Z",
    }])
    runner.qm.resting[TKR] = [RestingOrder(
        order_id="PAPER-1", market_ticker=TKR, side="yes", price_cents=45,
        size_contracts=25, placed_at=0.0, paper=True, client_order_id="LIP-1",
    )]
    kept = runner._risk_veto_pulls(TKR, {
        "action": "skip",
        "reason": "SENTINEL: rate_limit_global: 120 quotes in last 60s (cap 120)",
    })
    assert kept is False
    assert runner.qm.resting[TKR]
    assert runner.skip_counts["rate_limit_kept"] == 1
    pulled = runner._risk_veto_pulls(TKR, {
        "action": "skip",
        "reason": "SENTINEL: market_concentration: over the cap",
    })
    assert pulled is True
    assert not runner.qm.resting.get(TKR)


def test_amend_reserves_capital_and_refuses_a_cross_and_an_edge_price(tmp_path, monkeypatch):
    from engine.account_ledger import AccountLedger
    qm = QuoteManager(paper=True, db_path=_db(tmp_path),
                      account=AccountLedger(opening_cash_usd=100))
    order = RestingOrder(
        order_id="PAPER-1", market_ticker=TKR, side="yes", price_cents=40,
        size_contracts=10, placed_at=0.0, paper=True, client_order_id="LIP-a",
    )
    qm.account.reserve("LIP-a", market=TKR, program_id=TKR, price_cents=40, quantity=10)
    assert qm._amend_order(order, 0, 10) is False
    assert qm._amend_order(order, 100, 10) is False
    assert order.price_cents == 40
    assert qm._amend_order(order, 80, 10, best_opposing_bid_cents=50) is False
    assert order.price_cents == 40
    held = float(qm.account.reservations()[0].amount_usd)
    assert 4.0 <= held < 5.0
    assert qm._amend_order(order, 60, 20, best_opposing_bid_cents=30) is True
    assert order.price_cents == 60 and order.size_contracts == 20
    assert float(qm.account.reservations()[0].amount_usd) == pytest.approx(12.0, abs=0.5)

    posts = []
    qm.paper = False
    qm.client = SimpleNamespace(post=lambda path, body: posts.append(body) or {})
    monkeypatch.setattr(
        "execution.quote_manager.require_live_execution_allowed",
        lambda *a, **k: None)
    live = RestingOrder(
        order_id="LIVE-1", market_ticker=TKR, side="yes", price_cents=40,
        size_contracts=10, placed_at=0.0, paper=False, client_order_id="LIP-b",
    )
    # 2026-10-01: live amend is refused outright (legacy QuoteManager is paper-only).
    with pytest.raises(RuntimeError, match="paper-only"):
        qm._amend_order(live, 70, 10, best_opposing_bid_cents=40)
    assert posts == []
    assert live.price_cents == 40


def test_sizer_clamps_to_the_sentinel_concentration_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", _db(tmp_path))
    monkeypatch.setattr(settings, "BANKROLL_USD", 5000.0)
    monkeypatch.setattr(settings, "MAX_GROSS_PER_MARKET_USD", 1_000_000.0)
    monkeypatch.setattr(settings, "MAX_GROSS_PER_SERIES_USD", 1_000_000.0)
    monkeypatch.setattr(settings, "MAX_TOTAL_GROSS_USD", 1_000_000.0)
    monkeypatch.setenv("LIP_SINGLE_FILL_CAP_USD", "100000")
    from run_paper import PaperRunner
    market = dict(
        id="P-cap", market_ticker=TKR, target_size=100, discount_factor=0.5,
        reward_per_day_usd=20000.0, period_reward_usd=20000.0, period_seconds=86400.0,
        start_date="2026-01-01T00:00:00Z", end_date="2028-01-01T00:00:00Z",
    )
    runner = PaperRunner([market])
    book = BookState(market_ticker=TKR)
    book.yes_bids = [BookLevel(50, 300.0)]
    book.no_bids = [BookLevel(50, 300.0)]
    book.snapshot_count = 1
    # 1.5 × 800 = 1200. At 50¢ the $500 concentration cap allows 1000.
    sel = runner._economic_choice(book, runner.params_by_ticker[TKR], 50, 50, 800, 24.0)
    assert sel is not None
    sizes = [e.candidate.size_contracts for e in sel.considered if not e.candidate.is_no_quote]
    assert sizes
    assert max(sizes) <= 1000
    assert 1200 not in sizes


def test_paper_main_connects_the_fill_simulator(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", _db(tmp_path))
    from run_paper import PaperRunner, attach_paper_fills
    runner = PaperRunner([{
        "id": "p1", "market_ticker": TKR, "target_size": 100,
        "discount_factor": 0.5, "reward_per_day_usd": 10,
        "period_reward_usd": 10, "period_seconds": 86400,
        "start_date": "2026-01-01T00:00:00Z", "end_date": "2028-01-01T00:00:00Z",
    }])
    holder = {}

    class WS:
        def on_trade(self, cb):
            holder["cb"] = cb

    sim = attach_paper_fills(runner, WS())
    assert runner.fill_sim is sim
    book = BookState(market_ticker=TKR)
    book.yes_bids = [BookLevel(45, 0.0)]
    sim.track(order_id="o1", market_ticker=TKR, side="yes", price_cents=45,
              size=10, book=book, now=0.0)
    seen = []
    runner.on_fill = lambda ev: seen.append(ev)
    trade = {
        "trade_id": "t1", "ticker": TKR, "count_fp": "10",
        "yes_price_dollars": "0.45", "no_price_dollars": "0.55",
        "taker_side": "no", "created_time": "2030-01-01T00:00:00Z",
    }
    asyncio.run(holder["cb"](trade))
    assert len(seen) == 1 and seen[0].order_id == "o1"
    assert seen[0].exchange_ts is not None


def test_cancelled_paper_orders_are_untracked():
    from tools.run_session import untrack_cancelled
    sim = PaperFillSimulator(latency_ms=0.0)
    book = BookState(market_ticker=TKR)
    book.yes_bids = [BookLevel(45, 0.0)]
    sim.track(order_id="gone", market_ticker=TKR, side="yes", price_cents=45,
              size=10, book=book, now=0.0)
    sim.track(order_id="stay", market_ticker="OTHER", side="yes", price_cents=45,
              size=10, book=book, now=0.0)
    untrack_cancelled(sim, TKR, set())
    assert "gone" not in sim.orders
    assert "stay" in sim.orders
    fills = sim.apply_trades([{
        "trade_id": "t1", "ticker": TKR, "count_fp": "10",
        "yes_price_dollars": "0.45", "no_price_dollars": "0.55",
        "taker_side": "no", "created_time": "2030-01-01T00:00:00Z",
    }])
    assert fills == []


def test_as_guard_uses_trade_time(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", _db(tmp_path))
    from run_paper import PaperRunner
    runner = PaperRunner([{
        "id": "p1", "market_ticker": TKR, "target_size": 100,
        "discount_factor": 0.5, "reward_per_day_usd": 10,
        "period_reward_usd": 10, "period_seconds": 86400,
        "start_date": "2026-01-01T00:00:00Z", "end_date": "2028-01-01T00:00:00Z",
    }])

    def fake_apply(*args, **kwargs):
        runner.qm.last_fill_status = "applied"
        return SimpleNamespace(size_contracts=5)

    runner.qm.apply_fill = fake_apply
    stamped = []
    runner.as_guard.record_fill = lambda ticker, side, price, qty, ts: stamped.append(ts)
    ev = FillEvent(
        order_id="o", market_ticker=TKR, side="yes", count=5,
        price_cents_exact=45.0, is_taker=False, trade_id="",
        ts=111.0, exchange_ts=222.0,
    )
    runner.on_fill(ev)
    assert stamped == [222.0]
    stamped.clear()
    ev.ts = 333.0
    ev.exchange_ts = None
    runner.on_fill(ev)
    assert stamped == [333.0]


def test_init_db_creates_fill_and_settlement_tables(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "fill_ledger" in names
    assert "settlement_log" in names


def test_pytest_ini_collects_only_tests():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "pytest.ini").read_text()
    assert "testpaths" in text
    assert "tests" in text
