"""PM US Liquidity Incentive rules, pinned to hand-computed examples from the
official docs (fetched 2026-10-02):
  FAQ  https://docs.polymarket.us/incentives/liquidity
  API  https://docs.polymarket.us/api-reference/incentives/overview
  PAGE https://polymarket.us/rewards
Book mapping (engine): yes_bids = PM bids, no_bids = 100 - PM offers (cents).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine.lip_scorer import (BookLevel, BookState, OurQuotes, ProgramParams,
                               score_snapshot, snapshot_share)
from mm.selector import KalshiMarket, pmus_side_share, reward_per_day
from mm.unattended import pmus_paper as P
from polymarket.engine import pm_us_lip_scorer as S


def _snap(yes, no, ours_yes=(), ours_no=(), *, target, df=0.5, ms=None):
    book = BookState(market_ticker="PMUS:t",
                     yes_bids=sorted([BookLevel(p, q) for p, q in yes], key=lambda l: -l.price_cents),
                     no_bids=sorted([BookLevel(p, q) for p, q in no], key=lambda l: -l.price_cents))
    ours = OurQuotes(yes_bids=[BookLevel(p, q) for p, q in ours_yes],
                     no_bids=[BookLevel(p, q) for p, q in ours_no])
    params = ProgramParams(market_ticker="PMUS:t", target_size=target, discount_factor=df,
                           period_reward_usd=100.0, rules="pmus", max_spread_usd=ms)
    return score_snapshot(book, ours, params)


# ---- Rule: Score = DF^(ticks from best) x size, pro-rata within the side ----
def test_faq_scoring_example_df_030():
    # FAQ: 1,000 at best/1/2/3 ticks, DF 0.30 -> 1000, 300, 90, 27; best earns
    # 1000/1417 = 70.6%, 3 ticks away earns 27/1417 = 1.9%.
    side = [S.Order(0.50, 1000, ours=True), S.Order(0.49, 1000), S.Order(0.48, 1000), S.Order(0.47, 1000)]
    r = S.score_side(side, is_bid=True, tick=0.01, discount_factor=0.30, target_size=4000)
    assert r.qualified and r.total == pytest.approx(1417.0)
    assert r.share == pytest.approx(1000 / 1417)            # 0.7057
    far = [S.Order(o.price, o.size, ours=(o.price == 0.47)) for o in side]
    assert S.score_side(far, is_bid=True, tick=0.01, discount_factor=0.30,
                        target_size=4000).share == pytest.approx(27 / 1417)   # 0.019
    # engine path, same numbers
    s = _snap([(50, 1000), (49, 1000), (48, 1000), (47, 1000)], [], ours_yes=[(50, 1000)], target=4000, df=0.30)
    assert s.our_yes_normalized == pytest.approx(1000 / 1417)


def test_ticks_measured_from_that_sides_best_price_on_asks():
    # asks 51c (others 1000) and 52c (ours 1000) -> no_bids 49 and 48; ours 1 tick away.
    s = _snap([], [(49, 1000), (48, 1000)], ours_no=[(48, 1000)], target=2000, df=0.30)
    assert s.our_no_normalized == pytest.approx(300 / 1300)


# ---- Rule: Target Size walk (aggregate, raw size, whole levels, cutoff) ----
def test_faq_target_reached_before_our_level_scores_zero():
    # FAQ: Target 20,000 with 25,000 at best -> orders at the second-best price score 0.
    s = _snap([(50, 25000), (49, 1000)], [], ours_yes=[(49, 1000)], target=20000)
    assert s.yes_qualified and s.our_yes_normalized == 0.0


def test_straddling_level_scores_whole_and_deeper_levels_do_not():
    # Target 1,500: 1,000 at 50c (others), level 49c = 400 others + 600 ours.
    # The walk reaches 2,000 at 49c (API: "one whole price level at a time"),
    # so all of 49c scores: ours 0.5*600 = 300 of 1000 + 0.5*1000 = 1500 -> 0.2.
    s = _snap([(50, 1000), (49, 1000), (48, 5000)], [], ours_yes=[(49, 600), (48, 5000)], target=1500)
    assert s.yes_cutoff_price == 49
    assert s.our_yes_normalized == pytest.approx(300 / 1500)   # 48c (beyond the walk) adds nothing


def test_target_uses_raw_not_discounted_size():
    # 1,000 at 50c + 1,000 at 49c: raw 2,000 >= target 2,000 (discounted would be 1,500).
    s = _snap([(50, 1000), (49, 1000)], [], target=2000, df=0.5)
    assert s.yes_qualified and s.yes_cutoff_price == 49


def test_side_short_of_target_scores_zero_other_side_still_pays_without_max_spread():
    # bids total 900 < target 1,000 -> bid side 0; asks reach target and we
    # hold 500 of 1,000 at the best ask -> 0.5 on that side -> 0.25 of the second.
    s = _snap([(50, 900)], [(48, 500), (47, 500)], ours_yes=[(50, 900)], ours_no=[(48, 500)], target=1000)
    assert not s.yes_qualified and s.our_yes_normalized == 0.0
    assert s.no_qualified and s.our_no_normalized == pytest.approx(500 / 750)  # 500 / (500 + 0.5*500)
    assert snapshot_share(s) == pytest.approx(0.5 * 500 / 750)


def test_not_a_per_person_cap_and_sole_provider_earns_everything():
    # Ours 10,000 with others 2,500 at the same price, target 2,500 -> 0.8 of the side.
    s = _snap([(50, 12500)], [], ours_yes=[(50, 10000)], target=2500)
    assert s.our_yes_normalized == pytest.approx(0.8)
    # Only provider, meeting target on both sides -> the whole second.
    s = _snap([(50, 3000)], [(49, 3000)], ours_yes=[(50, 3000)], ours_no=[(49, 3000)], target=2500)
    assert snapshot_share(s) == pytest.approx(1.0)


def test_selector_side_share_matches_hand_value():
    # Book 2,000 at 50c; we add 100 at 51c (improve 1 tick), target 2,000, DF 0.5:
    # ours 100 / (100 + 0.5*2000) = 1/11. Walk reaches 2,100 at 50c.
    assert pmus_side_share([(50, 2000)], 51, 100, 2000, 0.5) == pytest.approx(100 / 1100)
    # Book too thin for target even with us -> 0.
    assert pmus_side_share([(50, 1000)], 51, 100, 2000, 0.5) == 0.0


# ---- Rule: Max Spread only when the API sends it (FAQ worked examples) ----
@pytest.mark.parametrize("bid,ask,paid", [(49, 51, True), (49, 57, False), (47, 54, True)])
def test_faq_max_spread_examples(bid, ask, paid):
    # Max Spread 3.5c, Target 1,000; asks at A cents -> no_bids at 100 - A.
    s = _snap([(bid, 1000)], [(100 - ask, 1000)], ours_yes=[(bid, 500)], target=1000, ms=0.035)
    assert s.snapshot_valid is paid
    assert snapshot_share(s) == (pytest.approx(0.25) if paid else 0.0)


def test_max_spread_side_short_of_target_pays_nobody():
    s = _snap([(50, 1000)], [(48, 10)], ours_yes=[(50, 1000)], target=1000, ms=0.035)
    assert snapshot_share(s) == 0.0
    # Same book, no Max Spread: the bid side pays on its own (1.0 of the side -> 0.5).
    s = _snap([(50, 1000)], [(48, 10)], ours_yes=[(50, 1000)], target=1000, ms=None)
    assert snapshot_share(s) == pytest.approx(0.5)


NOW = datetime(2026, 10, 2, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()


def _tp(**kw):
    tp = {"programId": "p1", "programType": "liquidityProgram", "status": "active",
          "start": "2026-10-01T00:00:00Z", "rewardPool": 40.0, "discountFactor": 0.5,
          "targetSize": 2500, "period": "daily_event"}
    tp.update(kw)
    return tp


def _rec(slug, *tps):
    return {"marketSlug": slug, "instrumentState": "INSTRUMENT_STATE_OPEN", "category": "CUL",
            "timePeriods": list(tps)}


def _meta():
    return {"close_ts": NOW + 15 * 86400, "tick": 0.01, "market_type": "futures", "category": "culture",
            "active": True, "closed": False, "occurrence_ts": None, "best_bid": 0.3, "best_ask": 0.56,
            "fetched": NOW}


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.delenv("LIP_PMUS_PERIODS", raising=False)
    monkeypatch.setenv("LIP_PMUS_POOL_SPLIT", "members")
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")


def test_max_spread_field_passed_only_when_present(_env):
    recs = [_rec("rtc-a-2026-10-20-x", _tp(programId="a", maxSpread=0.035)),
            _rec("rtc-b-2026-10-20-x", _tp(programId="b"))]
    frames, _st, _ = P.records_to_programs(recs, {r["marketSlug"]: _meta() for r in recs}, now=NOW)
    by = {f["market"]: f["max_spread_usd"] for f in frames}
    assert by == {"PMUS:rtc-a-2026-10-20-x": 0.035, "PMUS:rtc-b-2026-10-20-x": None}


# ---- Rule: volumeProgram periods never count for maker scoring ----
def test_volume_programs_are_ignored(_env):
    vol = {"programId": "v", "programType": "volumeProgram", "status": "active", "rewardPool": 5000.0,
           "minTakerNotional": 100, "period": "daily_event", "start": "2026-10-01T00:00:00Z"}
    recs = [_rec("rtc-v-2026-10-20-x", vol),
            _rec("rtc-l-2026-10-20-x", _tp(programId="v"), dict(vol))]
    frames, st, _ = P.records_to_programs(recs, {r["marketSlug"]: _meta() for r in recs}, now=NOW)
    assert st["reasons"].get("no_program") == 1
    assert [f["market"] for f in frames] == ["PMUS:rtc-l-2026-10-20-x"]
    assert frames[0]["period_reward_usd"] == pytest.approx(40.0)    # liquidity pool, 1 member
    assert S.count_pool_members(recs) == {("v", "daily_event"): 1}  # volume twin not counted
    assert [p.program_id for p in S.parse_incentives(recs[1])] == ["v"] and \
        S.parse_incentives(recs[0]) == []


# ---- Rule: pool shared across the program's markets (never summed) ----
def test_pool_is_shared_across_program_members(_env):
    # $100/day program over 4 markets -> $25/day slice each; a 0.10 share -> $2.50/day.
    recs = [_rec(f"rtc-s-2026-10-20-{c}", _tp(programId="s", rewardPool=100.0)) for c in "abcd"]
    frames, _st, _ = P.records_to_programs(recs, {r["marketSlug"]: _meta() for r in recs}, now=NOW)
    assert [f["period_reward_usd"] for f in frames] == [pytest.approx(25.0)] * 4
    m = _km(pool=frames[0]["period_reward_usd"], period_s=frames[0]["period_seconds"], left=3600.0)
    assert reward_per_day(0.10, m) == pytest.approx(2.50)


# ---- Rule: $1 minimum per (market, ET date) on the FULL day ----
def _km(pool, period_s=86400.0, left=86400.0):
    return KalshiMarket(market="PMUS:x", series="PMUS:x", period_reward_usd=pool, period_seconds=period_s,
                        seconds_left=left, discount_factor=0.5, target_size=2500, venue="pmus")


def test_min_payout_applies_to_the_full_day_not_the_rest_of_today():
    # $40/day slice, share 0.05 -> $2.00 per market-day: paid, at any hour.
    for left in (86400.0, 6 * 3600.0, 600.0):
        assert reward_per_day(0.05, _km(40.0, left=left)) == pytest.approx(2.0)
    # share 0.02 -> $0.80 per market-day: under $1, not paid.
    assert reward_per_day(0.02, _km(40.0)) == 0.0
    # exactly $1.00 is paid ("under $1.00" is not).
    assert reward_per_day(0.025, _km(40.0)) == pytest.approx(1.0)


def test_min_payout_multi_day_period_is_per_day():
    # early period: $70 over 7 days -> $10/day slice; share 0.10 -> $1.00/day paid,
    # share 0.09 -> $0.90/day not paid (even though the 7-day total is $6.30).
    assert reward_per_day(0.10, _km(70.0, period_s=7 * 86400.0, left=3 * 86400.0)) == pytest.approx(1.0)
    assert reward_per_day(0.09, _km(70.0, period_s=7 * 86400.0, left=3 * 86400.0)) == 0.0


def test_min_payout_short_period_is_whole_period():
    # 6 h period, $8 pool, share 0.10 -> $0.80 for the period: not paid.
    assert reward_per_day(0.10, _km(8.0, period_s=6 * 3600.0, left=3600.0)) == 0.0
    # share 0.25 -> $2.00 for the period: paid, $8/day rate while it runs.
    assert reward_per_day(0.25, _km(8.0, period_s=6 * 3600.0, left=3600.0)) == pytest.approx(8.0)


def test_kalshi_reward_path_unchanged():
    k = KalshiMarket(market="K", series="K", period_reward_usd=40.0, period_seconds=86400.0,
                     seconds_left=6 * 3600.0, discount_factor=0.5, target_size=2500)
    # Kalshi: floor on the remaining-window payout (0.05*40*0.25 = $0.50 < $1) -> 0.
    assert reward_per_day(0.05, k) == 0.0
