"""Per-second LIP accrual, payout matching, and series shrinkage.

The $16 figure is the Help Center example retrieved 2026-10-01: a $100
period, share 0.20, 8,000 of 10,000 snapshots qualifying. The halving
example is hand arithmetic on the same reference and discount rules.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from engine.lip_accrual import (
    RestingOrder, ScoringBook, SecondAccrual, book_for_score,
    max_reward_usd_from_centi_cents, payout_from_shares, period_payout,
    rule_version,
)
from engine.lip_calibration import RatioObs, error_distribution, series_factors
from engine.lip_reconcile import (
    PAYOUT_ENDPOINT, EstimateRow, credits_from_ledger, program_metadata, reconcile,
)
from engine.lip_scorer import ProgramParams, score_snapshot, snapshot_share
from execution.kalshi_ws import BookLevel, BookState
from mm.selector import KalshiMarket, allocate, quote_economics, reward_per_day
from mm.unattended.optimize import optimize_sizes


def _ts(year, month, day) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


AUG = _ts(2026, 8, 1)


def _params(**kw) -> ProgramParams:
    base = dict(
        market_ticker="MKT", target_size=100, discount_factor=0.5,
        period_reward_usd=10.0, program_id="prog", period_seconds=10,
        start_ts=float(AUG), end_ts=float(AUG + 10),
    )
    base.update(kw)
    return ProgramParams(**base)


def _level(cents: int, size: float) -> list[str]:
    return [f"{cents / 100:.4f}", f"{size:.2f}"]


def _snap(seq, yes=None, no=None, sid=1):
    return {
        "type": "orderbook_snapshot", "sid": sid, "seq": seq,
        "msg": {
            "market_ticker": "MKT",
            "yes_dollars_fp": yes or [],
            "no_dollars_fp": no or [],
        },
    }


def _delta(seq, side, cents, size, sid=1):
    return {
        "type": "orderbook_delta", "sid": sid, "seq": seq,
        "msg": {
            "market_ticker": "MKT", "side": side,
            "price_dollars": f"{cents / 100:.4f}",
            "delta_fp": f"{size:.2f}",
        },
    }


def _both(size: float) -> list[RestingOrder]:
    return [
        RestingOrder("yes", 50, size),
        RestingOrder("no", 50, size),
    ]


def test_help_center_example_scales_the_pool_by_qualifying_snapshots():
    shares = [Decimal("0.2")] * 8000 + [Decimal("0")] * 2000
    raw, payable = payout_from_shares(
        shares, period_reward_usd=Decimal("100"), period_seconds=10000)
    assert raw == Decimal("16")
    assert payable == Decimal("16")


def test_dollar_floor_applies_once_to_the_period_sum():
    raw, payable = payout_from_shares(
        [Decimal("0.4"), Decimal("0.4"), Decimal("0.4")],
        period_reward_usd=Decimal("3"), period_seconds=3)
    assert raw == Decimal("1.2")
    assert payable == Decimal("1.2")
    under, dropped = payout_from_shares(
        [Decimal("0.4")], period_reward_usd=Decimal("1"), period_seconds=1)
    assert under == Decimal("0.4")
    assert dropped == Decimal("0")


def test_account_cap_is_max_reward_per_account_before_the_floor():
    assert max_reward_usd_from_centi_cents(100_000) == Decimal("10")
    raw, payable = payout_from_shares(
        [Decimal("1"), Decimal("1")],
        period_reward_usd=Decimal("20"), period_seconds=2,
        max_reward_usd=Decimal("10"))
    assert raw == Decimal("20")
    assert payable == Decimal("10")
    assert period_payout(Decimal("0.50"), Decimal("10")) == Decimal("0")


def test_halving_one_tick_below_the_reference():
    # Target 100, DF 0.5. YES: 20 at 51¢ then our 80 at 50¢.
    # Reference is 51 (20 >= 100/5). Cutoff is 50. Our YES score is
    # 0.5 * 80 = 40 against 20 + 40. NO is entirely ours.
    book = BookState(market_ticker="MKT", yes_bids=[BookLevel(51, 20)])
    orders = [RestingOrder("yes", 50, 80), RestingOrder("no", 40, 100)]
    scored, ours = book_for_score(book, orders)
    snap = score_snapshot(scored, ours, _params())
    assert snap.yes_cutoff_price == 50
    assert snap.our_yes_normalized == pytest.approx(40 / 60)
    assert snap.our_no_normalized == pytest.approx(1)
    assert snapshot_share(snap) == pytest.approx((40 / 60 + 1) / 2)


def test_resting_size_is_not_inferred_and_filled_size_does_not_earn():
    book = BookState(
        market_ticker="MKT",
        yes_bids=[BookLevel(50, 100)],
        no_bids=[BookLevel(50, 100)],
    )
    scored, ours = book_for_score(book, _both(40))
    snap = score_snapshot(scored, ours, _params())
    assert snapshot_share(snap) == pytest.approx(0.4)
    assert scored.yes_bids[0].size == pytest.approx(100)

    # The public book has not echoed us yet, so none of this level is ours.
    empty = BookState(market_ticker="MKT", yes_bids=[BookLevel(50, 60)],
                      no_bids=[BookLevel(50, 60)])
    not_echoed = [
        RestingOrder("yes", 50, 40, in_book=0),
        RestingOrder("no", 50, 40, in_book=0),
    ]
    scored, ours = book_for_score(empty, not_echoed)
    snap = score_snapshot(scored, ours, _params())
    assert snap.snapshot_valid
    assert snapshot_share(snap) == pytest.approx(0.4)

    filled = book_for_score(book, [
        RestingOrder("yes", 50, 0, in_book=40),
        RestingOrder("no", 50, 0, in_book=40),
    ])
    thin = ProgramParams("MKT", target_size=50, discount_factor=0.5,
                         period_reward_usd=10, period_seconds=10,
                         start_ts=float(AUG), end_ts=float(AUG + 10))
    snap = score_snapshot(filled[0], filled[1], thin)
    assert snap.snapshot_valid
    assert snapshot_share(snap) == 0


def test_each_wall_clock_second_uses_the_end_of_second_book():
    accrual = SecondAccrual(_params(period_reward_usd=4, period_seconds=2))
    accrual.set_resting([
        RestingOrder("yes", 50, 100, in_book=0),
        RestingOrder("no", 50, 100, in_book=0),
    ])
    accrual.on_message(_snap(1), AUG + 0.1)
    accrual.on_message(_delta(2, "yes", 50, 100), AUG + 0.8)
    mark = accrual.score_second(AUG)
    assert mark.intra_second
    assert mark.share == pytest.approx(0.75)
    assert accrual.on_message(_delta(3, "yes", 50, 100), AUG + 0.9) == "late"
    assert accrual.late_messages == 1
    assert accrual.marks[0].share == pytest.approx(0.75)


def test_sequence_gap_is_unknown_until_a_snapshot():
    accrual = SecondAccrual(_params())
    accrual.set_resting(_both(100))
    accrual.on_message(_snap(1), AUG + 0.1)
    first = accrual.score_second(AUG)
    assert first.status == "scored"
    assert first.share == pytest.approx(1)
    assert accrual.on_message(_delta(3, "yes", 50, 10), AUG + 1.2) == "gap"
    assert accrual.book.needs_resync
    missed = accrual.score_second(AUG + 1)
    assert missed.status == "unknown"
    assert not missed.counted
    accrual.on_message(_snap(4, yes=[_level(50, 0)], no=[_level(50, 0)]), AUG + 2.2)
    assert accrual.book.needs_resync is False
    resumed = accrual.score_second(AUG + 2)
    assert resumed.status == "scored"
    estimate = accrual.estimate()
    assert estimate.unknown_seconds == 1
    assert estimate.known_seconds == 2
    assert Decimal(estimate.raw_usd) == pytest.approx(Decimal("2"))


def test_skipped_seconds_and_disconnect_are_not_counted():
    accrual = SecondAccrual(_params())
    accrual.set_resting(_both(100))
    accrual.on_message(_snap(1), AUG + 0.1)
    accrual.score_second(AUG)
    accrual.score_second(AUG + 3)
    estimate = accrual.estimate()
    assert estimate.unknown_seconds == 2
    assert estimate.known_seconds == 2
    assert Decimal(estimate.raw_usd) == Decimal("2")

    dark = SecondAccrual(_params())
    dark.set_resting(_both(100))
    dark.on_message(_snap(1), AUG + 0.1)
    dark.book.note_disconnect()
    mark = dark.score_second(AUG)
    assert mark.status == "unknown"
    assert dark.estimate().estimated_usd == "0"


def test_program_boundaries_and_rule_version():
    assert rule_version(float(_ts(2026, 7, 30))) == "2026_07_30"
    assert rule_version(float(_ts(2026, 7, 29))) == "pre_2026_07_30"
    edge = SecondAccrual(_params(start_ts=AUG + 0.5, end_ts=AUG + 10))
    edge.set_resting(_both(100))
    edge.on_message(_snap(1), AUG + 0.6)
    assert edge.score_second(AUG).status == "boundary"
    assert edge.score_second(AUG + 1).status == "scored"
    closed = SecondAccrual(_params(end_ts=AUG + 2))
    closed.set_resting(_both(100))
    closed.on_message(_snap(1), AUG + 0.1)
    closed.score_second(AUG)
    closed.score_second(AUG + 1)
    assert closed.score_second(AUG + 2).status == "out_of_program"
    old = SecondAccrual(_params(
        start_ts=float(_ts(2026, 2, 28)),
        end_ts=float(_ts(2026, 2, 28) + 10),
    ))
    old.set_resting(_both(100))
    old.on_message(_snap(1), _ts(2026, 2, 28) + 0.1)
    assert old.score_second(_ts(2026, 2, 28)).status == "unsupported_rule"
    assert old.estimate().rule_version == "pre_2026_07_30"
    assert old.estimate().estimated_usd == "0"


def test_reconciler_uses_tagged_credits_only():
    assert PAYOUT_ENDPOINT is None
    estimates = [EstimateRow("MKT", "prog", "KXBRENT", Decimal("16"))]
    credits, rejected = credits_from_ledger([
        {"kind": "liquidity_reward", "source": "kalshi_api", "market": "MKT",
         "program_id": "prog", "amount_usd": "12.00"},
        {"kind": "balance", "amount_usd": "5.00", "source": "kalshi_api"},
        {"kind": "settlement", "source": "kalshi_api", "market": "MKT",
         "program_id": "prog", "amount_usd": "9.00"},
        {"kind": "liquidity_reward", "source": "model_estimate", "market": "MKT",
         "program_id": "prog", "amount_usd": "16"},
    ])
    assert len(credits) == 1
    assert {row["reason"] for row in rejected} == {
        "not_a_liquidity_reward", "untagged_source"}
    meta = program_metadata({"market_ticker": "MKT", "id": "prog", "paid_out": True,
                             "period_reward": 1_000_000})
    assert meta["payout_usd"] is None
    assert meta["paid_out"] is True
    matched = reconcile(estimates, credits)
    assert matched["payout_endpoint"] is None
    assert matched["matches"][0].ratio == "0.75"
    assert matched["matches"][0].paid_usd == "12.00"
    assert matched["matches"][0].series == "KXBRENT"


def test_series_factor_shrinks_toward_one_and_scales_the_selector():
    obs = [
        RatioObs("KXBRENT", Decimal("10"), Decimal("12")),
        RatioObs("KXBRENT", Decimal("10"), Decimal("12")),
        RatioObs("KXBRENT", Decimal("10"), Decimal("12")),
        RatioObs("KXHIGH", Decimal("10"), Decimal("8")),
    ]
    factors = series_factors(obs)
    assert factors["KXBRENT"] == pytest.approx((3 * 1.2 + 5 * 1.0) / 8)
    assert factors["KXHIGH"] == pytest.approx((1 * 0.8 + 5 * 1.0) / 6)
    assert series_factors([]) == {}
    report = error_distribution(obs)
    assert report["count"] == 4
    assert report["median_ratio"] == pytest.approx(1.2)
    assert report["mean_abs_error_usd"] == pytest.approx(2)

    market = KalshiMarket(
        market="KXBRENT-26OCT07", series="KXBRENT",
        period_reward_usd=100, period_seconds=86400, seconds_left=86400,
        discount_factor=0.5, target_size=100, days_to_settle=3,
        exchange_index=2, shard_cash_usd=10_000,
    )
    base = reward_per_day(1.0, market)
    scaled = reward_per_day(1.0, market, reward_factor=factors["KXBRENT"])
    assert scaled == pytest.approx(base * factors["KXBRENT"])
    _net, _cap, share, _y, _n = quote_economics(market, 100, reward_factor=2)
    assert share == pytest.approx(1)
    picked = allocate(
        [market], bankroll=10_000, chunk=100, max_size=100,
        per_market_usd=500, per_series_usd=2_000, per_category_usd=5_000,
        series_factors={"KXBRENT": 0},
    )
    assert picked.taken == []
    sized = optimize_sizes(
        [market], bankroll=10_000, per_market_usd=10_000,
        per_event_usd=10_000, total_usd=10_000,
        sizes=(50, 100), markout_usd_per_contract=0.10,
        series_factors={"KXBRENT": 0},
    )
    assert sized.chosen == []


def test_scoring_book_does_not_apply_a_delta_across_a_gap():
    book = ScoringBook("MKT")
    assert book.apply_message(_snap(1, yes=[_level(50, 10)], no=[_level(40, 10)])) == "snapshot"
    assert book.apply_message(_delta(3, "yes", 50, 5)) == "gap"
    assert book.book.stale
    assert book.book.yes_bids[0].size == pytest.approx(10)
    assert book.apply_message(_delta(4, "yes", 50, 5)) == "stale_drop"
    assert book.book.yes_bids[0].size == pytest.approx(10)
