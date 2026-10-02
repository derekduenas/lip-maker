"""Patch 18: inventory skew replaces the fill cooldown when LIP_SKEW_ENABLE=1."""
from mm.unattended import loop as L
from mm.unattended import skew as S
from tests.test_patch15 import T0, _book, _prog

P = S.SkewParams(max_ticks=2, max_backoff=1, max_reward_loss=0.5)


def test_flat_inventory_is_unchanged():
    out = S.skew_prices(40, 55, net_yes=0, frac=0.0, best_yes=40, best_no=55, df=0.5, params=P)
    assert (out["yes_cents"], out["no_cents"], out["agg"], out["back"]) == (40, 55, 0, 0)


def test_long_yes_raises_no_and_backs_off_yes():
    out = S.skew_prices(40, 55, net_yes=50, frac=1.0, best_yes=40, best_no=55, df=0.5, params=P)
    assert out["reduce"] == "no" and out["add"] == "yes"
    assert out["no_cents"] == 57 and out["yes_cents"] == 39
    assert out["reward_loss"] == 0.5


def test_small_inventory_moves_both_sides_one_tick():
    # ceil on both sides: any inventory backs the adding side off one tick
    # (floor used to leave it untouched until the cap).
    out = S.skew_prices(40, 55, net_yes=-5, frac=0.2, best_yes=40, best_no=55, df=0.5, params=P)
    assert out["yes_cents"] == 41 and out["no_cents"] == 54 and out["back"] == 1


def test_never_crosses_the_book():
    # long NO -> raise YES; opposite (NO) best bid 58 => YES must stay <= 41
    out = S.skew_prices(41, 55, net_yes=-50, frac=1.0, best_yes=41, best_no=58, df=0.5, params=P)
    assert out["yes_cents"] + 58 <= 99
    out = S.skew_prices(40, 59, net_yes=-50, frac=1.0, best_yes=40, best_no=59, df=0.5, params=P)
    assert out["yes_cents"] == 40 and out["agg"] == 0


def test_backoff_limited_by_reward_loss():
    tight = S.SkewParams(max_ticks=2, max_backoff=3, max_reward_loss=0.3)
    out = S.skew_prices(40, 55, net_yes=50, frac=1.0, best_yes=40, best_no=55, df=0.5, params=tight)
    assert out["back"] == 0  # one tick at DF 0.5 already costs 50% > 30%
    loose = S.SkewParams(max_ticks=2, max_backoff=3, max_reward_loss=0.8)
    out = S.skew_prices(40, 55, net_yes=50, frac=1.0, best_yes=40, best_no=55, df=0.5, params=loose)
    assert out["back"] == 2 and abs(out["reward_loss"] - 0.75) < 1e-9
    assert S.reward_cost_per_tick(0.5, 3) == [0.5, 0.75, 0.875]


def test_fill_requotes_with_skew_instead_of_cooldown(monkeypatch):
    monkeypatch.setenv("LIP_SKEW_ENABLE", "1")
    monkeypatch.setenv("LIP_FILL_COOLDOWN_S", "1800")
    monkeypatch.setenv("LIP_MARKET_INV_CAP_USD", "25")
    monkeypatch.setenv("LIP_CROSS_GUARD", "1")
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-26DEC-T1")
    _book(loop, "KXA-26DEC-T1", [(40, 3000)], [(55, 3000)], T0 + 1)
    assert loop._quote("KXA-26DEC-T1", 40, 55, 100, T0 + 2)
    loop._note_fill({"market_ticker": "KXA-26DEC-T1", "side": "yes", "count": 20, "price_cents": 40},
                    T0 + 5)
    q = loop.resting["KXA-26DEC-T1"]
    # $8 of $25 cap => frac .32: NO +1 tick, YES backed off 1 tick (ceil)
    assert q["yes"] > 0 and q["no"] > 0
    assert q["no_cents"] == 56 and q["yes_cents"] == 39
    assert loop._side_blocked("KXA-26DEC-T1", "yes", T0 + 100) == ""
    assert loop.skew_stats["requotes"] == 1
    assert loop._skew_status()["skewed_now"] == 1


def test_cap_still_blocks_adding_side_under_skew(monkeypatch):
    monkeypatch.setenv("LIP_SKEW_ENABLE", "1")
    monkeypatch.setenv("LIP_MARKET_INV_CAP_USD", "5")
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-26DEC-T1")
    _book(loop, "KXA-26DEC-T1", [(40, 3000)], [(55, 3000)], T0 + 1)
    assert loop._quote("KXA-26DEC-T1", 40, 55, 100, T0 + 2)
    loop._note_fill({"market_ticker": "KXA-26DEC-T1", "side": "yes", "count": 20, "price_cents": 40},
                    T0 + 5)
    q = loop.resting["KXA-26DEC-T1"]
    assert q["yes"] == 0 and q["no"] > 0 and q["no_cents"] == 57


def test_repeg_keeps_skew_and_disabled_is_legacy(monkeypatch):
    monkeypatch.setenv("LIP_SKEW_ENABLE", "1")
    monkeypatch.setenv("LIP_REPEG_MIN_S", "1")
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-26DEC-T1")
    _book(loop, "KXA-26DEC-T1", [(40, 3000)], [(55, 3000)], T0 + 1)
    loop.position["KXA-26DEC-T1"] = {"yes": 0.0, "no": 30.0, "yes_cost": 0.0, "no_cost": 16.5}
    assert loop._quote("KXA-26DEC-T1", 40, 55, 100, T0 + 2)
    assert loop.resting["KXA-26DEC-T1"]["yes_cents"] == 42  # long NO => YES aggressive
    n0 = loop.repegs_n
    loop._guard_resting(T0 + 5)  # unchanged book: skewed target == current, no churn
    assert loop.repegs_n == n0
    monkeypatch.setenv("LIP_SKEW_ENABLE", "0")
    loop2 = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop2, "KXA-26DEC-T1")
    _book(loop2, "KXA-26DEC-T1", [(40, 3000)], [(55, 3000)], T0 + 1)
    loop2.position["KXA-26DEC-T1"] = {"yes": 0.0, "no": 30.0, "yes_cost": 0.0, "no_cost": 16.5}
    assert loop2._quote("KXA-26DEC-T1", 40, 55, 100, T0 + 2)
    assert loop2.resting["KXA-26DEC-T1"]["yes_cents"] == 40
    assert loop2._skew_status() == {"enabled": False}
