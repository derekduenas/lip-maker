"""Economic selection: does this quote beat not quoting?

The behaviour under test is the one the operating loop lacked. It sized to
the program's reward target and ranked by reward share or capital, which is
not profit: a market can pay the biggest rebate in the book and still lose
money once the fill, the fee and the unwind are counted.
"""
from __future__ import annotations

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import fees
from engine.quote_economics import (
    QuoteCandidate, evaluate, select, DEFAULT_OPERATING_COST_PER_QUOTE_USD)

BASE = dict(
    market_id="KXECON-26SEP30-T1",
    horizon_sec=86400.0,
    pool_rate_usd_per_sec=100.0 / 86400.0,   # $100/day pool
    target_size=100.0,
    discount_factor=0.5,
    top_book_size=200,
    midpoint=0.5,
    hours_to_settle=24.0,
    calibration=1.0,
)


def _ev(size=50, yes=45, no=50, **kw):
    args = dict(BASE); args.update(kw)
    return evaluate(QuoteCandidate(size, yes, no), **args)


# ── the baseline ──────────────────────────────────────────────────────────

def test_not_quoting_is_exactly_zero():
    e = _ev(size=0, yes=None, no=None)
    assert e.net_usd == D(0)
    assert e.candidate.is_no_quote


def test_every_term_is_reported_even_when_zero():
    e = _ev()
    d = e.explain()
    for k in ("expected_reward_usd", "expected_trading_pnl_usd",
              "expected_fees_usd", "expected_exit_cost_usd",
              "operating_cost_usd", "uncertainty_allowance_usd"):
        assert k in d


# ── double counting ───────────────────────────────────────────────────────

def test_adverse_selection_is_subtracted_exactly_once():
    """MarketYield.expected_daily_rebate is already net of adverse cost. The
    reward term must therefore be GROSS, with adverse appearing only as the
    trading-P&L line."""
    from cross_venue.yield_equation import MarketYield
    e = _ev(size=50)
    my = MarketYield(market_id=BASE["market_id"], pool_per_day=100.0,
                     our_size=50, top_book_size=200, target_size=100,
                     discount_factor=0.5, hours_to_settle=24.0,
                     midpoint=0.5, calibration=1.0)
    # reward is gross: strictly greater than the already-netted figure
    assert float(e.expected_reward_usd) > my.expected_daily_rebate
    # and the trading P&L is exactly the negative of the adverse cost
    assert float(e.expected_trading_pnl_usd) == pytest.approx(
        -my.adverse_cost_per_day, rel=1e-6)


def test_trading_pnl_is_never_positive_for_a_passive_maker():
    assert _ev().expected_trading_pnl_usd <= D(0)


# ── unknowns ──────────────────────────────────────────────────────────────

def test_unknown_fill_rate_is_declared_not_assumed_zero():
    e = _ev(expected_fills_per_horizon=None, fee_schedule=fees.active_schedule())
    assert any("expected_fills" in u for u in e.unknowns)
    # fees are priced, not silently zero
    assert e.expected_fees_usd > D(0)


def test_more_unknowns_means_a_bigger_allowance():
    known = _ev(expected_fills_per_horizon=2.0, observed_share=0.3,
                fee_schedule=fees.active_schedule())
    unknown = _ev(expected_fills_per_horizon=None, observed_share=None,
                  fee_schedule=None)
    assert unknown.uncertainty_allowance_usd > known.uncertainty_allowance_usd


def test_allowance_is_capped():
    e = _ev(expected_fills_per_horizon=None, observed_share=None, fee_schedule=None)
    assert e.uncertainty_allowance_usd <= e.expected_reward_usd * D("0.75")


# ── the decision ──────────────────────────────────────────────────────────

def test_a_quote_that_loses_money_is_not_chosen():
    """Tiny pool, real costs: not quoting wins."""
    sel = select([QuoteCandidate(50, 45, 50), QuoteCandidate(0, None, None)],
                 available_capital_usd=D(5000),
                 **{**BASE, "pool_rate_usd_per_sec": 0.000001},
                 fee_schedule=fees.active_schedule())
    assert not sel.should_quote
    assert "no-quote" in sel.reason or "beats not quoting" in sel.reason


def test_a_clearly_profitable_quote_is_chosen():
    sel = select([QuoteCandidate(50, 45, 50), QuoteCandidate(0, None, None)],
                 available_capital_usd=D(5000),
                 **{**BASE, "pool_rate_usd_per_sec": 5000.0 / 86400.0},
                 expected_fills_per_horizon=1.0,
                 fee_schedule=fees.active_schedule())
    assert sel.should_quote
    assert sel.chosen.net_usd > D(0)


def test_candidates_that_do_not_fit_capital_are_discarded():
    sel = select([QuoteCandidate(5000, 45, 50), QuoteCandidate(0, None, None)],
                 available_capital_usd=D("10"),
                 **{**BASE, "pool_rate_usd_per_sec": 5000.0 / 86400.0},
                 fee_schedule=fees.active_schedule())
    assert not sel.should_quote
    assert "capital" in sel.reason


def test_selection_prefers_net_per_dollar_not_gross_reward():
    """Two sizes: the larger earns more gross reward but ties up more
    capital. Under one shared account the better rate per dollar wins."""
    small, large = QuoteCandidate(20, 45, 50), QuoteCandidate(400, 45, 50)
    sel = select([small, large, QuoteCandidate(0, None, None)],
                 available_capital_usd=D(5000),
                 **{**BASE, "pool_rate_usd_per_sec": 5000.0 / 86400.0},
                 expected_fills_per_horizon=1.0,
                 fee_schedule=fees.active_schedule())
    chosen = sel.chosen
    others = [e for e in sel.considered
              if not e.candidate.is_no_quote and e.candidate != chosen.candidate]
    for o in others:
        assert chosen.net_per_capital >= o.net_per_capital


def test_our_own_size_dilutes_our_share():
    """Our quote is part of the aggregate depth, so doubling size does not
    double the reward — that competitive effect is why the best candidate is
    usually not the largest."""
    a = _ev(size=50, pool_rate_usd_per_sec=5000.0 / 86400.0)
    b = _ev(size=100, pool_rate_usd_per_sec=5000.0 / 86400.0)
    assert float(b.expected_reward_usd) < 2 * float(a.expected_reward_usd)


def test_one_sided_quote_is_refused():
    e = _ev(size=50, yes=45, no=None)
    assert e.net_usd <= D(0) and e.unknowns


def test_operating_cost_is_charged():
    e = _ev()
    assert e.operating_cost_usd == DEFAULT_OPERATING_COST_PER_QUOTE_USD


# ── wired into the runner ─────────────────────────────────────────────────

import sqlite3
import time
from unittest.mock import MagicMock

from config import settings
from execution.kalshi_ws import BookLevel, BookState
from run_paper import PaperRunner

TKR = "KXECONRUN-26SEP30-T1"


@pytest.fixture
def econ_db(tmp_path, monkeypatch):
    path = tmp_path / "econ.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE lip_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_ticker TEXT, captured_at TEXT,
            our_score REAL, total_score REAL, yes_qualified INTEGER, no_qualified INTEGER,
            snapshot_valid INTEGER, estimated_payout_usd REAL, was_resting INTEGER,
            our_share REAL);
        CREATE TABLE fill_ledger (
            trade_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT, side TEXT,
            count INTEGER, yes_price_cents INTEGER, no_price_cents INTEGER,
            is_taker INTEGER, created_at TEXT, synced_at TEXT);
        CREATE TABLE settlement_log (ticker TEXT PRIMARY KEY);
    """)
    conn.commit(); conn.close()
    monkeypatch.setattr(settings, "DB_PATH", str(path))
    return str(path)


def _runner(econ_db, pool):
    m = dict(id="P-econ", market_ticker=TKR, target_size=100,
             discount_factor=0.5, reward_per_day_usd=pool,
             period_reward_usd=pool, period_seconds=86400.0,
             start_date="2026-01-01T00:00:00Z", end_date="2028-01-01T00:00:00Z")
    r = PaperRunner([m])
    r.qm.paper = True
    r.qm.db_path = econ_db
    r.qm.resting = {}
    return r


def _book():
    b = BookState(market_ticker=TKR)
    b.yes_bids = [BookLevel(45, 300.0)]
    b.no_bids = [BookLevel(50, 300.0)]
    b.snapshot_count = 1
    return b


def test_runner_rejects_an_uneconomic_quote(econ_db):
    """A pool of $0.01/day cannot pay for the fee and the unwind."""
    r = _runner(econ_db, pool=0.01)
    sel = r._economic_choice(_book(), r.params_by_ticker[TKR], 45, 50, 50, 24.0)
    assert sel is not None, "economic layer did not run"
    assert not sel.should_quote, f"uneconomic quote accepted: {sel.reason}"


def test_runner_accepts_a_richly_paid_quote(econ_db):
    r = _runner(econ_db, pool=20000.0)
    sel = r._economic_choice(_book(), r.params_by_ticker[TKR], 45, 50, 50, 24.0)
    assert sel is not None and sel.should_quote, \
        f"profitable quote rejected: {sel.reason if sel else 'no selection'}"
    assert sel.chosen.net_usd > 0


def test_runner_never_chooses_more_than_the_account_can_fund(econ_db):
    """Most of the account is committed elsewhere. The quote must size down
    to what is actually free rather than assume the whole balance."""
    r = _runner(econ_db, pool=20000.0)
    r.account.reserve("hold", market="X", program_id="P",
                      price_cents=99, quantity=5000)       # $4,950 held
    free = D(str(r.account.available_usd()))
    sel = r._economic_choice(_book(), r.params_by_ticker[TKR], 45, 50, 50, 24.0)
    assert sel.chosen.capital_usd <= free, \
        "chose a quote the shared account cannot fund"
    if sel.should_quote:
        assert sel.chosen.candidate.size_contracts < 50   # sized down


def test_runner_refuses_when_no_candidate_fits(econ_db):
    """With the account fully committed, not quoting is the only option."""
    r = _runner(econ_db, pool=20000.0)
    r.account.reserve("hold", market="X", program_id="P",
                      price_cents=99, quantity=5050)       # ~$4,999.50 held
    sel = r._economic_choice(_book(), r.params_by_ticker[TKR], 45, 50, 50, 24.0)
    assert not sel.should_quote
    assert "capital" in sel.reason


def test_unknown_share_is_passed_through_as_unknown(econ_db):
    r = _runner(econ_db, pool=20000.0)
    sel = r._economic_choice(_book(), r.params_by_ticker[TKR], 45, 50, 50, 24.0)
    quoting = [e for e in sel.considered if not e.candidate.is_no_quote]
    assert any(any("observed_share" in u for u in e.unknowns) for e in quoting)


# ── exposure limits on qualifying sizes ───────────────────────────────────

def test_event_exposure_limit_discards_correlated_candidates():
    """Strikes on one event are mutually exclusive outcomes: quoting several
    is ONE correlated bet. A qualifying size is not exempt."""
    sel = select([QuoteCandidate(200, 45, 50), QuoteCandidate(0, None, None)],
                 available_capital_usd=D(5000),
                 max_event_capital_usd=D("20"),
                 event_capital_used_usd=D("19"),
                 **{**BASE, "pool_rate_usd_per_sec": 5000.0 / 86400.0},
                 expected_fills_per_horizon=1.0,
                 fee_schedule=fees.active_schedule())
    assert not sel.should_quote
    assert "exposure limits" in sel.reason


def test_qualification_is_not_forced_past_the_limits():
    """Reaching the target is what makes reward non-zero, but it must not
    override the account: if it does not fit, we do not quote."""
    sel = select([QuoteCandidate(5000, 45, 50), QuoteCandidate(0, None, None)],
                 available_capital_usd=D("50"),
                 **{**BASE, "pool_rate_usd_per_sec": 50000.0 / 86400.0},
                 expected_fills_per_horizon=1.0,
                 fee_schedule=fees.active_schedule())
    assert not sel.should_quote


def test_event_key_groups_strikes_of_one_event(econ_db):
    r = _runner(econ_db, pool=100.0)
    assert r._event_key("KXTEMPMIAH-26SEP2101-T72.99") == "KXTEMPMIAH-26SEP2101"
    assert (r._event_key("KXTEMPMIAH-26SEP2101-T70.99")
            == r._event_key("KXTEMPMIAH-26SEP2101-T72.99"))
    assert r._event_key("KXTEMPMIAH-26SEP2200-T70.99") != \
        r._event_key("KXTEMPMIAH-26SEP2101-T70.99")


def test_event_capital_counts_only_other_strikes_of_the_same_event(econ_db):
    r = _runner(econ_db, pool=100.0)
    a = "KXEV-26SEP21-T1"
    r.account.reserve("o1", market="KXEV-26SEP21-T2", program_id="p",
                      price_cents=50, quantity=10)          # same event
    r.account.reserve("o2", market="KXOTHER-26SEP21-T1", program_id="p",
                      price_cents=50, quantity=10)          # different event
    r.account.reserve("o3", market=a, program_id="p",
                      price_cents=50, quantity=10)          # this market
    used = r._event_capital_used(a)
    # Only the sibling strike counts: not this market, not the other event.
    # A direct reserve() holds the premium alone; the maker-fee allowance is
    # added by the placement path, not here.
    assert used == D("5.00")


# ── turnover consistency ──────────────────────────────────────────────────

def test_adverse_selection_scales_with_turnover_like_fees():
    """Charging a fee on every fill while charging adverse selection once
    made the model inconsistent exactly where it mattered: a small quote
    refilled thousands of times showed thousands of fees against a single
    day of adverse selection."""
    one = _ev(expected_fills_per_horizon=1.0, fee_schedule=fees.active_schedule())
    many = _ev(expected_fills_per_horizon=100.0, fee_schedule=fees.active_schedule())
    fee_ratio = many.expected_fees_usd / one.expected_fees_usd
    pnl_ratio = many.expected_trading_pnl_usd / one.expected_trading_pnl_usd
    assert fee_ratio == pytest.approx(float(pnl_ratio), rel=1e-6)


def test_one_fill_reproduces_the_unscaled_holding_cost():
    from cross_venue.yield_equation import MarketYield
    e = _ev(size=50, expected_fills_per_horizon=1.0)
    my = MarketYield(market_id=BASE["market_id"], pool_per_day=100.0,
                     our_size=50, top_book_size=200, target_size=100,
                     discount_factor=0.5, hours_to_settle=24.0,
                     midpoint=0.5, calibration=1.0)
    assert float(e.expected_trading_pnl_usd) == pytest.approx(
        -my.adverse_cost_per_day, rel=1e-6)


def test_sub_one_turnover_does_not_discount_adverse_selection():
    """A fill rate below one must not make the position look safer than
    holding it once."""
    low = _ev(expected_fills_per_horizon=0.01)
    one = _ev(expected_fills_per_horizon=1.0)
    assert low.expected_trading_pnl_usd == one.expected_trading_pnl_usd
