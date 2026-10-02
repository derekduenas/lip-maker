"""Review fixes (venue area): selection economics - adverse selection counted
once, skew back-off, conservative fee types, PM US rebate rounding."""
import pytest

from mm import selector as SEL
from mm.unattended import screen as S

FRAME = {"market": "KXFOO-26DEC-T1", "series": "KXFOO", "period_reward_usd": 50,
         "period_seconds": 86400, "target_size": 50}
META = {"yes_bid": 0.40, "yes_ask": 0.45, "yes_bid_size": 200, "yes_ask_size": 200,
        "volume_24h": 5000}


def _km(**kw):
    base = dict(market="KXFOO-26DEC-T1", series="KXFOO", period_reward_usd=50, period_seconds=86400,
                seconds_left=86400, discount_factor=0.5, target_size=50,
                yes_bids=[(40, 200)], no_bids=[(55, 200)], fee_type="quadratic",
                days_to_settle=1.0, exchange_index=0)
    base.update(kw)
    return SEL.KalshiMarket(**base)


# ------------------------------------------------------------------ item 5
def _as_in_net(km, size=100.0):
    net, _cap, share, _y, _n = SEL.quote_economics(km, size)
    # fee_type quadratic (no maker fee), days_to_settle 1 (no holding):
    # everything net subtracts from the reward is the adverse-selection term.
    return SEL.reward_per_day(share, km) - net


def test_markout_prior_is_charged_once(monkeypatch):
    # No volume / time / news multiplier: the screen adds nothing on top of net.
    monkeypatch.setenv("LIP_RANK_SHORT_K", "0")
    meta = dict(META, volume_24h=0)
    km = _km()
    prior_charge = 100 * 2 * SEL.FILL_FRACTION_PER_DAY["event"] * abs(SEL.MARKOUT_PRIOR_CENTS["event"]) / 100
    assert _as_in_net(km) == pytest.approx(prior_charge)
    pen = S.rank_score(FRAME, meta, category="Economics", days=1.0)["penalty"]
    assert pen == pytest.approx(0.0)
    assert _as_in_net(km) + pen == pytest.approx(prior_charge)  # once, not twice


def test_rank_penalty_is_the_incremental_part_per_100_contracts(monkeypatch):
    monkeypatch.setenv("LIP_RANK_SHORT_K", "3")
    monkeypatch.setenv("LIP_RANK_NEWS_MULT", "2")
    km = _km()
    fill = SEL.FILL_FRACTION_PER_DAY["event"]
    adverse = abs(SEL.MARKOUT_PRIOR_CENTS["event"])
    vol_mult, time_mult, news_mult = 2.0, 1.0 + 3.0 / 1.0, 2.0
    full_100 = 100 * 2 * fill * vol_mult * adverse / 100 * time_mult * news_mult
    rk = S.rank_score(FRAME, META, category="Entertainment", days=1.0)
    # per-100 units even though target_size (50) < 100
    assert _as_in_net(km) + rk["penalty"] == pytest.approx(full_100)
    assert rk["penalty_unit_contracts"] == 100
    # the screen's own score still charges the full markout at its size S
    S_ = 50.0
    assert rk["penalty_full"] == pytest.approx(full_100 * S_ / 100)


# ------------------------------------------------------------------ item 6
def test_skew_backoff_starts_before_the_cap():
    from mm.unattended.skew import SkewParams, skew_prices
    p = SkewParams(max_ticks=2, max_backoff=1, max_reward_loss=0.5)
    for frac in (0.01, 0.3, 0.5, 0.99):
        o = skew_prices(40, 55, net_yes=+10, frac=frac, best_yes=40, best_no=55, df=0.5, params=p)
        assert o["back"] == 1 and o["yes_cents"] == 39, frac
    # still limited by the reward-loss bound (1 tick at DF 0.3 costs 70% > 50%)
    o = skew_prices(40, 55, net_yes=+10, frac=0.3, best_yes=40, best_no=55, df=0.3, params=p)
    assert o["back"] == 0 and o["yes_cents"] == 40
    # min_frac still gates everything
    q = SkewParams(max_ticks=2, max_backoff=1, max_reward_loss=0.5, min_frac=0.4)
    o = skew_prices(40, 55, net_yes=+10, frac=0.3, best_yes=40, best_no=55, df=0.5, params=q)
    assert o["back"] == 0 and o["agg"] == 0
