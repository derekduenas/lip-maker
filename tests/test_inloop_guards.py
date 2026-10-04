"""Gaps 4 and 5: in-loop adverse-selection guard (per-market markout EWMA widen/pull,
fill-burst pull) and competition/toxicity exits. Both opt-in, default off."""
import pytest

from mm.selector import KalshiMarket, competition_ratio, exit_reason
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap  # noqa: F401

BOOK_YES = [(40, 2000), (39, 2000)]
BOOK_NO = [(55, 2000), (54, 2000)]


def _quoting(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, BOOK_YES, BOOK_NO))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert M in lp.resting
    return lp


# ------------------------------------------------------------- EWMA math
def test_markout_ewma_is_quantity_weighted_and_decays(monkeypatch):
    lp = _quoting(monkeypatch)
    lp._as_note(M, 10, -2.0, T0 + 5)
    assert lp.mk_ewma[M][0] == pytest.approx(-2.0) and lp.mk_ewma[M][2] == 1
    lp._as_note(M, 30, -6.0, T0 + 6)
    ewma, weight, n = lp.mk_ewma[M]
    # decay 0.7 on the old weight: (0.7*10*-2 + 30*-6) / (0.7*10 + 30)
    assert ewma == pytest.approx((0.7 * 10 * -2.0 + 30 * -6.0) / (0.7 * 10 + 30)) and n == 2


# ------------------------------------------------------------- adverse-selection guard
def test_guard_is_off_by_default_and_only_measures(monkeypatch):
    lp = _quoting(monkeypatch)
    monkeypatch.delenv("LIP_AS_GUARD_ENABLE", raising=False)
    for k in range(5):
        lp._as_note(M, 10, -9.0, T0 + 5 + k)
    assert M in lp.resting and lp.pulls.get("as_toxic") is None and lp._as_back == {}


def test_toxic_markouts_pull_the_market_and_block_it_for_the_cooldown(monkeypatch):
    lp = _quoting(monkeypatch, LIP_AS_GUARD_ENABLE=1, LIP_AS_MIN_OBS=3, LIP_AS_PULL_CENTS=3,
                  LIP_AS_TOXIC_COOLDOWN_S=600)
    for k in range(2):
        lp._as_note(M, 10, -5.0, T0 + 5 + k)
    assert M in lp.resting                                   # below the minimum observations
    lp._as_note(M, 10, -5.0, T0 + 8)
    assert M not in lp.resting and lp.pulls["as_toxic"] == 1
    assert lp.cooldown[(M, "*")] == pytest.approx(T0 + 8 + 600)
    assert lp._policy_block(M, T0 + 100) == "move_cooldown"
    assert lp._policy_block(M, T0 + 700) == ""


def test_moderate_toxicity_backs_off_one_tick_then_recovers(monkeypatch):
    lp = _quoting(monkeypatch, LIP_AS_GUARD_ENABLE=1, LIP_AS_MIN_OBS=3, LIP_AS_WIDEN_CENTS=1,
                  LIP_AS_PULL_CENTS=3)
    y0, n0 = lp.resting[M]["yes_cents"], lp.resting[M]["no_cents"]
    for k in range(3):
        lp._as_note(M, 10, -1.5, T0 + 5 + k)
    assert lp._as_back[M] == 1 and M in lp.resting
    assert (lp.resting[M]["yes_cents"], lp.resting[M]["no_cents"]) == (y0 - 1, n0 - 1)
    for k in range(10):                                      # flow turns benign
        lp._as_note(M, 10, +1.0, T0 + 20 + k)
    assert M not in lp._as_back
    assert (lp.resting[M]["yes_cents"], lp.resting[M]["no_cents"]) == (y0, n0)


def test_sampling_group_is_measured_but_exempt_unless_asked(monkeypatch):
    lp = _quoting(monkeypatch, LIP_AS_GUARD_ENABLE=1, LIP_AS_MIN_OBS=3, LIP_AS_PULL_CENTS=3)
    lp.sample_markets.add(M)
    for k in range(4):
        lp._as_note(M, 10, -8.0, T0 + 5 + k)
    assert M in lp.resting and lp.mk_ewma[M][2] == 4 and lp.pulls.get("as_toxic") is None
    monkeypatch.setenv("LIP_AS_GUARD_SAMPLE", "1")
    lp._as_note(M, 10, -8.0, T0 + 20)
    assert M not in lp.resting and lp.pulls["as_toxic"] == 1


def test_same_side_fill_burst_pulls_both_sides(monkeypatch):
    lp = _quoting(monkeypatch, LIP_AS_GUARD_ENABLE=1, LIP_AS_BURST_CONTRACTS=100, LIP_AS_BURST_WINDOW_S=60,
                  LIP_AS_BURST_COOLDOWN_S=120)
    lp._as_burst(M, "yes", 60, T0 + 10)
    assert M in lp.resting
    lp._as_burst(M, "no", 60, T0 + 20)                       # other side: separate tally
    assert M in lp.resting
    lp._as_burst(M, "yes", 50, T0 + 30)                      # 110 yes within 60 s
    assert M not in lp.resting and lp.pulls["as_burst"] == 1
    assert lp.cooldown[(M, "*")] == pytest.approx(T0 + 30 + 120)


def test_markout_ewma_flows_in_from_a_measured_60s_markout(monkeypatch):
    lp = _quoting(monkeypatch, LIP_AS_GUARD_ENABLE=1)
    lp.on_frame(snap(M, T0 + 10, [(35, 2000)], [(63, 2000)]))   # yes mid 36: 5c below our 41c fill
    lp.fill_marks.append({"market": M, "side": "yes", "price_cents": 41.0, "count": 10.0, "ts": T0 + 2,
                          "mid0": 41.0, "venue": "kalshi", "bucket": "short", "synthetic": False,
                          "markout_60s": None, "markout_300s": None, "markout_1800s": None})
    lp._update_markouts(T0 + 65)
    assert lp.mk_ewma[M][0] == pytest.approx(-5.0) and lp.mk_ewma[M][2] == 1


def test_guard_state_persists(monkeypatch, tmp_path):
    lp = _quoting(monkeypatch, LIP_AS_GUARD_ENABLE=1)
    lp.attach_state(str(tmp_path / "s.json"))
    lp._as_note(M, 10, -2.0, T0 + 5)
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "s.json"))
    assert lp2.mk_ewma == lp.mk_ewma


# ------------------------------------------------------------- competition / toxicity exits
def _km(yes_total, no_total, **kw):
    return KalshiMarket(market=M, series="KXCPI", period_reward_usd=500, period_seconds=86400,
                        seconds_left=86400, discount_factor=0.5, target_size=1000,
                        yes_bids=[(40, yes_total)], no_bids=[(55, no_total)], **kw)


def test_competition_ratio_prefers_the_time_averaged_value():
    assert competition_ratio(_km(1000, 1000)) == pytest.approx(1.0)
    assert competition_ratio(_km(1000, 1000, competition_ewma=0.4)) == pytest.approx(0.4)


def test_exit_reason_fires_on_a_spike_only_relative_to_the_entry_baseline():
    km = _km(3000, 3000, entry_competition=1.0, competition_ewma=2.0)
    assert exit_reason(km) == "competition_spike"
    assert exit_reason(_km(3000, 3000, entry_competition=1.0, competition_ewma=1.4)) == ""


def test_exits_are_off_by_default(monkeypatch):
    lp = _quoting(monkeypatch)
    monkeypatch.delenv("LIP_EXITS_ENABLE", raising=False)
    assert lp.entry_state == {} and lp._km(M).entry_competition is None


def test_entry_baseline_is_recorded_at_first_quote_and_persisted(monkeypatch, tmp_path):
    lp = _quoting(monkeypatch, LIP_EXITS_ENABLE=1)
    assert M in lp.entry_state and lp.entry_state[M]["competition"] > 0
    km = lp._km(M)
    assert km.entry_competition == pytest.approx(lp.entry_state[M]["competition"]) and km.incumbent
    lp.attach_state(str(tmp_path / "s.json"))
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "s.json"))
    assert lp2.entry_state == lp.entry_state


def test_one_noisy_snapshot_does_not_exit_but_sustained_competition_does(monkeypatch):
    lp = _quoting(monkeypatch, LIP_EXITS_ENABLE=1, LIP_COMP_EWMA_ALPHA=0.3, LIP_EXIT_COOLDOWN_S=3600, LIP_EXIT_COMP_REL=1.0)
    base = lp.entry_state[M]["competition"]
    deep = lambda ts: snap(M, ts, BOOK_YES + [(20, 6400)], BOOK_NO + [(20, 6400)])   # far-away depth only
    lp.on_frame(deep(T0 + 10))                                                         # a one-off pile-on
    lp._select(T0 + 11)
    assert M in lp.resting and lp.pulls.get("exit:competition_spike") is None
    for k in range(8):                                                                 # it stays
        lp.on_frame(deep(T0 + 20 + k))
        lp._select(T0 + 30 + k)
    assert lp.comp_ewma[M] > base + 0.5
    assert M not in lp.resting and lp.pulls["exit:competition_spike"] == 1
    assert M not in lp.entry_state                         # fresh baseline only after the cooldown
    assert lp.cooldown[(M, "*")] > T0 + 3000


def test_toxicity_exit_when_the_series_markout_worsens_against_the_entry_baseline(monkeypatch):
    lp = _quoting(monkeypatch, LIP_EXITS_ENABLE=1, LIP_EMPIRICAL_MARKOUT_ENABLE=1)
    entry = lp.entry_state[M]["markout_cents"]
    lp.series_acc["KXCPI"] = {"fills": 10, "fees": 0.0, "settled_fills": 0, "settled_usd": 0.0,
                              "mk5_usd": -10.0, "mk5_contracts": 100.0, "mk5_n": 10}   # -10c per contract
    assert lp._km(M).entry_markout_cents == pytest.approx(entry)
    lp._select(T0 + 50)
    assert M not in lp.resting and lp.pulls["exit:toxicity"] == 1
