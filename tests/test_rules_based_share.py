"""Reward share under the PROGRAM'S rules, not a raw depth ratio.

engine/quote_economics used MarketYield.our_share:

    our_size / (top_book_size + our_size)

with `top_book_size` = best-yes depth + best-no depth. Measured on a live
book that understated our share by about 2x (ratio 1.679e-05 against a
rules-based 3.389e-05) because it summed depth across BOTH sides into a
single one-sided denominator.

It was also wrong in kind. Kalshi scores qualifying, distance-weighted
depth:

  * a side qualifies only when cumulative depth reaches TargetSize; that
    price is the CUTOFF;
  * the REFERENCE is the level reaching TargetSize/5;
  * levels at price >= cutoff score DiscountFactor^(reference-price) x size;
    levels below the cutoff score NOTHING;
  * each side normalises to 1.0, so a snapshot pays 2.0 and our fraction is
    our_total_score / 2.

Two consequences the ratio form gets backwards, both pinned here: deep
books do not automatically dilute, and we do not have to supply TargetSize
ourselves.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.lip_scorer import OurQuotes, ProgramParams, score_snapshot, snapshot_share
from engine.quote_economics import QuoteCandidate, rules_based_share
from execution.kalshi_ws import BookLevel, BookState

T = "KXSHARE-26SEP30-T1"


def _book(yes, no):
    b = BookState(market_ticker=T)
    b.yes_bids = sorted([BookLevel(p, float(s)) for p, s in yes],
                        key=lambda l: -l.price_cents)
    b.no_bids = sorted([BookLevel(p, float(s)) for p, s in no],
                       key=lambda l: -l.price_cents)
    b.snapshot_count = 1
    return b


def _share(book, size, yes_p=45, no_p=50, target=1000.0, df=0.5):
    return rules_based_share(book, QuoteCandidate(size, yes_p, no_p),
                             target_size=target, discount_factor=df)


# ── agreement with the scorer ─────────────────────────────────────────────

def test_matches_the_scorer_directly():
    book = _book([(45, 2000)], [(50, 2000)])
    got = _share(book, 100)
    ours = OurQuotes(yes_bids=[BookLevel(45, 100.0)], no_bids=[BookLevel(50, 100.0)])
    pp = ProgramParams(market_ticker=T, target_size=1000.0, discount_factor=0.5,
                       period_reward_usd=1.0, period_seconds=1.0)
    assert got == pytest.approx(snapshot_share(score_snapshot(book, ours, pp)))


# ── depth outside the cutoff must not dilute ──────────────────────────────

def test_depth_below_the_cutoff_does_not_dilute_us():
    """The claim this refutes: 'the book is 50x the target, so our share is
    diluted 50x'. Depth outside the cutoff earns nobody anything and must
    not appear in the denominator."""
    shallow = _book([(45, 2000)], [(50, 2000)])
    deep = _book([(45, 2000), (30, 90_000), (20, 90_000)],
                 [(50, 2000), (35, 90_000), (25, 90_000)])
    assert _share(deep, 100) == pytest.approx(_share(shallow, 100)), \
        "depth beyond the cutoff diluted our share"


def test_depth_inside_the_cutoff_does_dilute_us():
    thin = _book([(45, 2000)], [(50, 2000)])
    thick = _book([(45, 20_000)], [(50, 20_000)])
    assert _share(thick, 100) < _share(thin, 100)


# ── we need not supply the target ─────────────────────────────────────────

def test_a_tiny_order_still_earns_when_the_side_already_qualifies():
    """Existing liquidity counts toward qualification. A one-contract order
    inside the cutoff earns a share of an already-qualifying side."""
    book = _book([(45, 50_000)], [(50, 50_000)])
    s = _share(book, 1)
    assert s is not None and s > 0, \
        "a qualifying side paid nothing to an order inside the cutoff"


def test_supplying_the_whole_target_is_not_required_for_a_valid_snapshot():
    book = _book([(45, 50_000)], [(50, 50_000)])
    ours = OurQuotes(yes_bids=[BookLevel(45, 1.0)], no_bids=[BookLevel(50, 1.0)])
    pp = ProgramParams(market_ticker=T, target_size=1000.0, discount_factor=0.5,
                       period_reward_usd=1.0, period_seconds=1.0)
    assert score_snapshot(book, ours, pp).snapshot_valid


def test_a_side_that_never_reaches_target_pays_nobody():
    book = _book([(45, 10)], [(50, 10)])          # nowhere near 1000
    assert _share(book, 5) == 0.0


# ── distance weighting ────────────────────────────────────────────────────

# A book whose cutoff is several levels deep, so there is room to rest
# INSIDE the cutoff but away from the reference. With target 4000 the
# cutoff is 42 (cumulative reaches 4000 there) and the reference is 45
# (cumulative reaches target/5 = 800 at the first level).
_LADDER_YES = [(45, 1000), (44, 1000), (43, 1000), (42, 1000)]
_LADDER_NO = [(50, 1000), (49, 1000), (48, 1000), (47, 1000)]


def test_a_quote_below_the_cutoff_earns_exactly_nothing():
    """Not merely less — nothing. Only levels at or inside the cutoff score."""
    book = _book([(45, 2000)], [(50, 2000)])
    assert _share(book, 100, yes_p=43, no_p=48) == 0.0


def test_inside_the_cutoff_a_quote_further_from_reference_earns_less():
    book = _book(_LADDER_YES, _LADDER_NO)
    near = _share(book, 100, yes_p=45, no_p=50, target=4000.0)
    far = _share(book, 100, yes_p=43, no_p=48, target=4000.0)
    assert 0.0 < far < near


def test_discount_factor_controls_how_fast_distance_costs_us():
    book = _book(_LADDER_YES, _LADDER_NO)
    gentle = _share(book, 100, yes_p=43, no_p=48, target=4000.0, df=0.9)
    harsh = _share(book, 100, yes_p=43, no_p=48, target=4000.0, df=0.1)
    assert 0.0 < harsh < gentle


# ── plumbing ──────────────────────────────────────────────────────────────

def test_no_book_returns_none_so_the_caller_can_declare_it():
    assert _share(None, 100) is None


def test_no_quote_candidate_has_no_share():
    assert rules_based_share(_book([(45, 2000)], [(50, 2000)]),
                             QuoteCandidate(0, None, None),
                             target_size=1000.0, discount_factor=0.5) is None


def test_economics_labels_which_share_source_it_used():
    from engine import fees
    from engine.quote_economics import evaluate
    book = _book([(45, 2000)], [(50, 2000)])
    kw = dict(market_id=T, horizon_sec=3600.0, pool_rate_usd_per_sec=0.01,
              target_size=1000.0, discount_factor=0.5, top_book_size=4000,
              midpoint=0.5, hours_to_settle=1.0, calibration=1.0,
              fee_schedule=fees.active_schedule())
    with_book = evaluate(QuoteCandidate(100, 45, 50), book=book, **kw)
    without = evaluate(QuoteCandidate(100, 45, 50), **kw)
    assert with_book.assumptions["qualification"]["share_source"] == "program_rules"
    assert without.assumptions["qualification"]["share_source"] == "depth_ratio_fallback"
    assert any("depth ratio" in u for u in without.unknowns)
