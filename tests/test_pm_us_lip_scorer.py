"""PM US LIP scorer against the worked examples in
https://docs.polymarket.us/incentives/liquidity.md (retrieved 2026-09-30)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket.engine.pm_us_lip_scorer import (
    Order, expected_payout_usd, parse_incentives, payable, score_side, score_snapshot,
)


def test_doc_example_df_030_shares():
    # 1,000 at best, 1 tick, 2 ticks, 3 ticks away; DF 0.30
    bids = [Order(0.50, 1000, ours=True), Order(0.49, 1000), Order(0.48, 1000), Order(0.47, 1000)]
    r = score_side(bids, is_bid=True, tick=0.01, discount_factor=0.30, target_size=4000)
    assert r.qualified
    assert r.total == pytest.approx(1417)
    assert r.share == pytest.approx(1000 / 1417, rel=1e-6)          # 70.6%
    r3 = score_side([Order(0.50, 1000), Order(0.49, 1000), Order(0.48, 1000),
                     Order(0.47, 1000, ours=True)],
                    is_bid=True, tick=0.01, discount_factor=0.30, target_size=4000)
    assert r3.share == pytest.approx(27 / 1417, rel=1e-6)            # 1.9%


def test_target_reached_before_our_level_scores_zero():
    bids = [Order(0.50, 25000), Order(0.49, 5000, ours=True)]
    r = score_side(bids, is_bid=True, tick=0.01, discount_factor=0.5, target_size=20000)
    assert r.qualified and r.ours == 0.0


def test_side_short_of_target_does_not_qualify():
    r = score_side([Order(0.50, 500, ours=True)], is_bid=True, tick=0.01,
                   discount_factor=0.5, target_size=1000)
    assert not r.qualified and r.share == 0.0


def test_ask_side_distance_measured_upward_and_decicent_ticks():
    asks = [Order(0.512, 100), Order(0.514, 100, ours=True)]
    r = score_side(asks, is_bid=False, tick=0.001, discount_factor=0.5, target_size=200)
    assert r.ours == pytest.approx(25.0)          # 2 ticks away: 0.5^2 × 100
    assert r.size_adjusted_price == pytest.approx(0.514)


def _two_sided(bid_px, ask_px, size=1000):
    return [Order(bid_px, size, ours=True)], [Order(ask_px, size)]


def test_max_spread_examples_from_docs():
    b, a = _two_sided(0.49, 0.51)
    assert score_snapshot(b, a, tick=0.01, discount_factor=0.5, target_size=1000,
                          max_spread_usd=0.035).paid
    b, a = _two_sided(0.49, 0.57)                 # mid 0.53, each side 4c away
    r = score_snapshot(b, a, tick=0.01, discount_factor=0.5, target_size=1000,
                       max_spread_usd=0.035)
    assert not r.paid and r.our_share == 0.0
    b, a = _two_sided(0.47, 0.54)                 # exactly 3.5c each side: pays
    assert score_snapshot(b, a, tick=0.01, discount_factor=0.5, target_size=1000,
                          max_spread_usd=0.035).paid


def test_max_spread_forfeits_when_a_side_is_short():
    b = [Order(0.49, 1000, ours=True)]
    a = [Order(0.51, 10)]
    r = score_snapshot(b, a, tick=0.01, discount_factor=0.5, target_size=1000,
                       max_spread_usd=0.035)
    assert not r.paid and r.our_share == 0.0


def test_one_sided_earns_without_max_spread():
    b = [Order(0.49, 1000, ours=True)]
    r = score_snapshot(b, [], tick=0.01, discount_factor=0.5, target_size=1000)
    assert r.paid and r.our_share == pytest.approx(0.5)   # assumption A1


def test_payout_and_minimum():
    shares = [0.5] * 3600 + [0.0] * (86400 - 3600)
    usd = expected_payout_usd(shares, reward_pool_usd=100.0, period_seconds=86400)
    assert usd == pytest.approx(100 * 0.5 * 3600 / 86400)
    assert payable(0.99) == 0.0 and payable(2.0) == 2.0


def test_parse_gateway_record():
    rec = {"marketSlug": "astatc-nhl-ana-veg-2026-10-02-ftts-ana",
           "timePeriods": [
               {"programId": "p1", "programType": "liquidityProgram", "rewardPool": 40,
                "status": "active", "discountFactor": 0.3, "targetSize": 2000, "period": "live",
                "start": "2026-09-29T03:00:00Z"},
               {"programId": "p2", "programType": "liquidityProgram", "rewardPool": 40,
                "discountFactor": 0.25, "targetSize": 3000, "period": "day_of",
                "maxSpread": 0.035},
               {"programId": "bad", "programType": "liquidityProgram"},
               {"programId": "x", "programType": "volumeProgram", "rewardPool": 1,
                "discountFactor": 1, "targetSize": 1}]}
    progs = parse_incentives(rec)
    assert [p.program_id for p in progs] == ["p1", "p2"]
    assert progs[0].max_spread_usd is None and progs[1].max_spread_usd == 0.035
