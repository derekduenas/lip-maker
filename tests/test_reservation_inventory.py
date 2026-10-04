"""Gap 7: opt-in Avellaneda-Stoikov reservation skew in the engine (LIP_RESERVATION_ENABLE),
sigma from our own mid history, and an inventory-age reduce-only exit (LIP_INV_MAX_AGE_H).
Defaults are off; the linear tick skew is unchanged."""
import pytest
from hypothesis import given, settings, strategies as st

from mm.unattended import skew as SK
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap  # noqa: F401

DF = 0.5


def _res(**kw):
    base = dict(yes_c=40, no_c=55, net_yes=1.0, frac=0.5, best_yes=40, best_no=55, df=DF, yes_ref=40,
                no_ref=55, fair_cents=42.5, sigma_cents=5.0, tau_hours=6.0, gamma=0.04, max_skew=3,
                max_reward_loss=0.5)
    base.update(kw)
    return SK.reservation_prices(**base)


# ---------------------------------------------------------------- pure model
def test_long_yes_lowers_the_yes_bid_and_raises_the_no_bid():
    out = _res(net_yes=1.0, frac=1.0)             # q=1, sigma 5, tau 6 h -> shift 0.04*25*6 = 6c, capped to 3
    assert out["add"] == "yes" and out["reduce"] == "no"
    assert out["no_cents"] > 55 or out["no_cents"] == 99 - 40       # reducing side moves up (never through the ask)
    assert out["yes_cents"] < 40
    assert out["model"] == "reservation" and out["skew_cents"] == -3


def test_short_yes_is_the_mirror_image():
    out = _res(net_yes=-1.0, frac=1.0)
    assert out["add"] == "no" and out["reduce"] == "yes" and out["no_cents"] < 55 and out["yes_cents"] >= 40


def test_no_inventory_no_shift_and_zero_sigma_no_shift():
    assert _res(net_yes=0.0, frac=0.0)["yes_cents"] == 40
    out = _res(sigma_cents=0.0)
    assert (out["yes_cents"], out["no_cents"]) == (40, 55)


def test_backoff_of_the_adding_side_is_limited_by_the_reward_loss():
    # DF 0.5: one tick below the reference costs 50% of that side's credit, two cost 75%
    out = _res(net_yes=1.0, frac=1.0, max_reward_loss=0.5)
    assert out["back"] == 1 and out["yes_cents"] == 39
    out = _res(net_yes=1.0, frac=1.0, max_reward_loss=0.8)
    assert out["back"] == 2
    assert _res(net_yes=1.0, frac=1.0, max_reward_loss=0.0)["back"] == 0


def test_the_reducing_side_never_crosses_the_opposite_ask():
    out = _res(net_yes=1.0, frac=1.0, no_c=55, best_yes=44, best_no=55)   # yes touch 44 -> NO bid <= 99-44 = 55
    assert out["no_cents"] <= 99 - 44 and out["no_cents"] >= 55


def test_suppress_flag_at_the_cap():
    assert _res(net_yes=1.0, frac=1.0)["suppress"] == "yes"
    assert _res(net_yes=-1.0, frac=1.0)["suppress"] == "no"
    assert _res(net_yes=1.0, frac=0.5)["suppress"] is None


@settings(max_examples=150, deadline=None)
@given(yes=st.integers(2, 97), no=st.integers(2, 97), frac=st.floats(0.01, 1.5), sign=st.sampled_from([-1.0, 1.0]),
       sigma=st.floats(0.0, 15.0), tau=st.floats(0.01, 48.0), df=st.floats(0.1, 1.0), loss=st.floats(0.0, 1.0))
def test_property_prices_stay_in_range_and_never_cross(yes, no, frac, sign, sigma, tau, df, loss):
    if yes + no >= 100:
        return                                           # an uncrossed book only
    out = SK.reservation_prices(yes_c=yes, no_c=no, net_yes=sign * frac, frac=frac, best_yes=yes, best_no=no, df=df,
                                yes_ref=yes, no_ref=no, fair_cents=(yes + 100 - no) / 2.0, sigma_cents=sigma,
                                tau_hours=tau, gamma=0.04, max_skew=3, max_reward_loss=loss)
    assert 1 <= out["yes_cents"] <= 99 and 1 <= out["no_cents"] <= 99
    assert out["yes_cents"] + out["no_cents"] <= 99                       # an uncrossed book stays uncrossed
    assert yes - 3 <= out["yes_cents"] <= yes + 3 and no - 3 <= out["no_cents"] <= no + 3   # bounded shift
    assert (out["yes_cents"] - yes) * (out["no_cents"] - no) <= 0        # the sides move in opposite directions


# ---------------------------------------------------------------- sigma estimator
def test_sigma_from_mid_history_needs_enough_data_then_scales_with_moves():
    lp = newloop(bankroll=1500.0)
    assert lp._sigma_cents(M, T0) is None
    lp._note_mid(M, T0, 40.0)
    assert lp._sigma_cents(M, T0 + 100) is None
    quiet, wild = newloop(bankroll=1500.0), newloop(bankroll=1500.0)
    for i in range(40):
        quiet._note_mid(M, T0 + 60 * i, 40.0 + (0.1 if i % 2 else 0.0))
        wild._note_mid(M, T0 + 60 * i, 40.0 + (4.0 if i % 2 else 0.0))
    sq, sw = quiet._sigma_cents(M, T0 + 2400), wild._sigma_cents(M, T0 + 2400)
    assert sq is not None and sw is not None and sw > 10 * sq
    assert sw == pytest.approx(4.0 / (1.0 / 60.0) ** 0.5, rel=0.05)   # |dmid| 4c per minute -> per sqrt(hour)


# ---------------------------------------------------------------- engine wiring
def _held(monkeypatch, **env):
    monkeypatch.setenv("LIP_SKEW_ENABLE", "1")
    monkeypatch.setenv("LIP_MARKET_INV_CAP_USD", "25")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.position[M] = {"yes": 50.0, "no": 0.0, "yes_cost": 20.0, "no_cost": 0.0, "fees": 0.0, "venue": "kalshi"}
    return lp


def test_default_keeps_the_linear_tick_skew(monkeypatch):
    lp = _held(monkeypatch)
    monkeypatch.delenv("LIP_RESERVATION_ENABLE", raising=False)
    y, n, info = lp._skew_target(M, 40, 55)
    assert info is not None and info.get("model") is None


def test_enabled_uses_the_reservation_model_with_a_default_sigma_before_history(monkeypatch):
    lp = _held(monkeypatch, LIP_RESERVATION_ENABLE=1, LIP_SKEW_MAX_REWARD_LOSS=0.8)
    y, n, info = lp._skew_target(M, 40, 55)
    assert info["model"] == "reservation" and info["sigma_source"] == "default"
    assert info["add"] == "yes" and y <= 40 and n >= 55
    status = lp._skew_status()
    assert status["enabled"] is True and status["reservation"]["enabled"] is True


def test_reservation_off_without_inventory(monkeypatch):
    lp = _held(monkeypatch, LIP_RESERVATION_ENABLE=1)
    lp.position[M]["yes"] = 0.0
    lp.position[M]["yes_cost"] = 0.0
    assert lp._skew_target(M, 40, 55)[:2] == (40, 55)


# ---------------------------------------------------------------- inventory age
def test_inventory_age_is_tracked_and_cleared_when_flat(monkeypatch):
    lp = _held(monkeypatch)
    lp._note_inventory_age(M, T0 + 100)
    assert lp.inv_since[M] == T0 + 100
    lp._note_inventory_age(M, T0 + 500)
    assert lp.inv_since[M] == T0 + 100                       # first time wins
    lp.position[M]["yes"] = 0.0
    lp.position[M]["yes_cost"] = 0.0
    lp._note_inventory_age(M, T0 + 900)
    assert M not in lp.inv_since


def test_aged_inventory_blocks_the_adding_side_only_when_enabled(monkeypatch):
    lp = _held(monkeypatch)
    lp.inv_since[M] = T0
    monkeypatch.delenv("LIP_INV_MAX_AGE_H", raising=False)
    assert lp._side_blocked(M, "yes", T0 + 10 * 3600) == ""
    monkeypatch.setenv("LIP_INV_MAX_AGE_H", "6")
    assert lp._side_blocked(M, "yes", T0 + 5 * 3600) == ""
    assert lp._side_blocked(M, "yes", T0 + 7 * 3600) == "inventory_age"      # adding side: reduce-only
    assert lp._side_blocked(M, "no", T0 + 7 * 3600) == ""                    # the reducing side keeps quoting


def test_aged_inventory_pushes_the_reducing_side_to_the_maximum_skew(monkeypatch):
    lp = _held(monkeypatch, LIP_INV_MAX_AGE_H=6)
    lp.position[M]["yes"] = 5.0
    lp.position[M]["yes_cost"] = 2.0                         # tiny inventory: little skew normally
    lp.now = T0 + 100
    small = lp._inv_frac(M)
    lp.inv_since[M] = T0 - 7 * 3600
    assert lp._inv_frac(M) >= 1.0 > small


def test_inventory_age_persists_and_is_reported(monkeypatch, tmp_path):
    lp = _held(monkeypatch, LIP_INV_MAX_AGE_H=6)
    lp.attach_state(str(tmp_path / "s.json"))
    lp.now = T0 + 7 * 3600
    lp.inv_since[M] = T0
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "s.json"))
    assert lp2.inv_since == {M: T0}
    lp.position[M]["yes"] = 50.0
    rep = lp._skew_status()["inventory_age"]
    assert rep["max_age_h"] == 6 and rep["aged"][0]["market"] == M and rep["aged"][0]["age_h"] == pytest.approx(7.0)
