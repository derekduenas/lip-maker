"""lipforge/oct10-nogo-fix (COMMAND 2026-10-10, Oct 10 gate NO-GO; paper only):
(1) short bucket selected 0 / 51 alloc_no_positive_step: payable selection halved
one-sided shares and charged full costs against a 50%-uptime reward;
(2) series denylist; (3) 30-90c band enforced on the placed price + <10c favour."""
import pytest

from mm.unattended import loop as L
from mm.selector import exclusion_reason, quote_economics
from tests.test_review_loop_pnl import (  # noqa: F401  (autouse env fixture)
    M, T0, _env, _filled_loop, apply_policy, newloop, program, snap,
)

SHORT = "KXSHORT-26OCT15-T5"
# A thin cheap YES book (YES side at 8c: outside the band) and a NO side at
# 88-90c (inside the 30-90 band): the band leaves a one-sided YES quote.
YES = [(8, 400), (7, 2000)]
NO = [(88, 400), (87, 2000)]


def _short_program(reward):
    return program(SHORT, reward=reward, close_ts=T0 + 5 * 86400, days_to_settle=5)


def _one_sided_share():
    probe = newloop()
    probe.on_frame(_short_program(100.0))
    probe.on_frame(snap(SHORT, T0, YES, NO))
    km = next(k for k in probe._markets() if k.market == SHORT)
    return km, quote_economics(km, 100.0, sides=("yes",))[2]


def _pool_for_expect(expect):
    """Pool at which the CORRECT expected payout at 100 contracts one-sided
    is ``expect`` (accrued 0, uptime 0.5)."""
    km, share1 = _one_sided_share()
    assert share1 > 0
    return expect / (share1 * km.seconds_left / km.period_seconds * 0.5)


def test_one_sided_payable_share_is_not_halved(monkeypatch):
    """Reproduces the Oct 10 failure: correct expectation $2.40 (>= the $1.50
    floor), the old share2 x len(sides)/2 gave $1.20 -> reward zeroed."""
    monkeypatch.setenv("LIP_PAYABLE_SELECT", "1")
    monkeypatch.setenv("LIP_PAYABLE_UPTIME", "0.5")
    monkeypatch.setenv("LIP_MIN_PAYABLE_PER_PERIOD_USD", "1.5")
    monkeypatch.delenv("LIP_PAYABLE_UPTIME_COSTS", raising=False)
    pool = _pool_for_expect(2.4)
    lp = newloop()
    lp.on_frame(_short_program(pool))
    lp.on_frame(snap(SHORT, T0, YES, NO))
    km = next(k for k in lp._markets() if k.market == SHORT)
    net, _cap, share1, _y, _n = quote_economics(km, 100.0, sides=("yes",))
    net0 = quote_economics(km, 100.0, sides=("yes",), reward_factor=0.0)[0]
    halved = share1 / 2.0 * pool * km.seconds_left / km.period_seconds * 0.5
    assert halved < 1.5 <= 2 * halved                    # the old formula fell under the floor
    out = lp._payable_net(km, 100.0, ("yes",), net, share1)
    assert lp.payable_select_stats.get("below_floor", 0) == 0
    assert out == pytest.approx(net0 + (net - net0) * 0.5)


def test_uptime_haircut_scales_costs_too(monkeypatch):
    monkeypatch.setenv("LIP_PAYABLE_SELECT", "1")
    monkeypatch.setenv("LIP_PAYABLE_UPTIME", "0.5")
    monkeypatch.setenv("LIP_PAYABLE_UPTIME_COSTS", "1")
    pool = _pool_for_expect(3.0)
    lp = newloop()
    lp.on_frame(_short_program(pool))
    lp.on_frame(snap(SHORT, T0, YES, NO))
    km = next(k for k in lp._markets() if k.market == SHORT)
    net, _cap, share1, _y, _n = quote_economics(km, 100.0, sides=("yes",))
    net0 = quote_economics(km, 100.0, sides=("yes",), reward_factor=0.0)[0]
    assert lp._payable_net(km, 100.0, ("yes",), net, share1) == pytest.approx(0.5 * net)
    # below the floor nothing changes: costs only, at full rate
    tiny = newloop()
    tiny.on_frame(_short_program(pool / 10.0))
    tiny.on_frame(snap(SHORT, T0, YES, NO))
    km2 = next(k for k in tiny._markets() if k.market == SHORT)
    n2, _c, s2, _y, _n = quote_economics(km2, 100.0, sides=("yes",))
    n20 = quote_economics(km2, 100.0, sides=("yes",), reward_factor=0.0)[0]
    assert tiny._payable_net(km2, 100.0, ("yes",), n2, s2) == pytest.approx(n20)
    assert net0 < 0


def _deployed(monkeypatch):
    apply_policy(monkeypatch, grok=True)
    for k in ("LIP_FV_ENABLE", "LIP_PMUS_PAPER_ENABLE", "LIP_RECORD_ENABLE", "LIP_EVENT_CALENDAR_FILE",
              "LIP_ACTIVITY_WEIGHT", "LIP_FAST_ALLOCATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIP_SELECTION_DUMP", "off")


def test_short_bucket_one_sided_market_is_selected_under_the_deployed_policy(monkeypatch):
    """End to end: a 5-day market whose only quotable side is outside the band
    and whose correct expected payout clears the floor is now selected, in
    the short bucket (Oct 10: short bucket 0 selected)."""
    _deployed(monkeypatch)
    pool = _pool_for_expect(4.0)
    lp = newloop(bankroll=1500.0)
    lp.on_frame(_short_program(pool))
    lp.on_frame(snap(SHORT, T0, YES, NO))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert SHORT in lp.resting, (lp.cap_skips, lp.policy_skips, lp.rank_skips)
    assert lp.bucket_of[SHORT] == "short"
    q = lp.resting[SHORT]
    assert q["no"] == 0 and q["yes"] > 0                   # the in-band NO side never rests
    assert not lp.cap_skips


def test_series_denylist_excludes_and_blocks_quotes(monkeypatch):
    _deployed(monkeypatch)
    from mm.selector import series_denylist
    assert series_denylist() == {"KXHURCAT", "KXHURPATHAL", "KXTRUMPENDORSEMENTS", "KXNEXTTEAMNFL"}
    bad = "KXTRUMPENDORSEMENTS-26OCT20-A15"
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(bad))
    lp.on_frame(snap(bad, T0, [(5, 2000), (4, 2000)], [(93, 2000), (92, 2000)]))
    km = next(k for k in lp._markets() if k.market == bad)
    assert exclusion_reason(km) == "series_denylist"
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert bad not in lp.resting
    assert lp._quote(bad, 5, 93, 100, T0 + 2) is False      # direct path refuses too
    monkeypatch.setenv("LIP_SERIES_DENYLIST", "")
    assert exclusion_reason(km) != "series_denylist"


def test_band_is_enforced_on_the_placed_price(monkeypatch):
    monkeypatch.setenv("LIP_AVOID_BAND_LO", "30")
    monkeypatch.setenv("LIP_AVOID_BAND_HI", "90")
    monkeypatch.setenv("LIP_AVOID_BAND_REDUCE", "0")
    lp = newloop()
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(20, 2000), (19, 2000)], [(70, 2000), (69, 2000)]))
    # a caller (repeg, skew, sampling) asks for a 31c YES / 70c NO quote
    assert lp._quote(M, 31, 70, 50, T0 + 1) is False
    assert M not in lp.resting and lp.band_quote_drops_n == 1
    assert lp._quote(M, 20, 70, 50, T0 + 2) is True
    assert lp.resting[M]["no"] == 0 and lp.resting[M]["yes"] == 50
    st = lp.live_snapshot()["selection_policy"]
    assert st["avoid_band_cents"] == [30.0, 90.0] and st["avoid_band_reduce"] is False


def test_strict_band_blocks_reducing_side_unless_allowed(monkeypatch):
    lp = _filled_loop()                                    # long 100 YES @40: NO side reduces
    lp.on_frame(snap(M, T0 + 3, [(40, 2000)], [(55, 2000)]))
    monkeypatch.setenv("LIP_AVOID_BAND_LO", "30")
    monkeypatch.setenv("LIP_AVOID_BAND_HI", "90")
    monkeypatch.setenv("LIP_AVOID_BAND_REDUCE", "1")
    assert lp._side_blocked(M, "no", T0 + 4) == ""
    assert lp._band_sides(M, 40, 55, ("yes", "no")) == ("no",)
    monkeypatch.setenv("LIP_AVOID_BAND_REDUCE", "0")
    assert lp._side_blocked(M, "no", T0 + 4) == "price_band"
    assert lp._band_sides(M, 40, 55, ("yes", "no")) == ()


def test_low_price_boost_orders_only(monkeypatch):
    monkeypatch.delenv("LIP_FAVOR_LOW_CENTS", raising=False)
    assert L.low_price_boost(5, 93, ("yes", "no")) == 1.0
    monkeypatch.setenv("LIP_FAVOR_LOW_CENTS", "10")
    monkeypatch.setenv("LIP_FAVOR_LOW_BOOST", "1.0")
    assert L.low_price_boost(5, 93, ("yes", "no")) == 2.0
    assert L.low_price_boost(5, 93, ("no",)) == 1.0
    assert L.low_price_boost(12, 85, ("yes", "no")) == 1.0


def test_low_price_market_wins_the_budget(monkeypatch):
    """A <10c-side candidate next to a 20c one: the cheap one is selected."""
    _deployed(monkeypatch)
    monkeypatch.setenv("LIP_PAYABLE_SELECT", "0")
    lp = newloop(bankroll=1500.0)
    cheap, mid = "KXCHEAP-26NOV30-T1", "KXMID-26NOV30-T1"
    lp.on_frame(program(cheap, reward=2000.0))
    lp.on_frame(program(mid, reward=2000.0))
    lp.on_frame(snap(cheap, T0, [(6, 400), (5, 2000)], [(92, 400), (91, 2000)]))
    lp.on_frame(snap(mid, T0, [(20, 400), (19, 2000)], [(92, 400), (91, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert cheap in lp.resting


def test_deployed_policy_has_the_oct10_flags(monkeypatch):
    _deployed(monkeypatch)
    import os
    assert os.environ["LIP_PAYABLE_UPTIME_COSTS"] == "1"
    assert os.environ["LIP_AVOID_BAND_REDUCE"] == "0"
    assert os.environ["LIP_AVOID_BAND_LO"] == "30" and os.environ["LIP_AVOID_BAND_HI"] == "90"
    assert os.environ["LIP_FAVOR_LOW_CENTS"] == "10"
    assert os.environ["LIP_LIVE_ACK"] if "LIP_LIVE_ACK" in os.environ else True
    assert "LIP_LIVE_ACK" not in os.environ
    assert L.resolve_mode({"LIP_PAPER": "true", "LIP_FORCE_PAPER": "1"}) == "paper"
