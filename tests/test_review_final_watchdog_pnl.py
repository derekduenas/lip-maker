"""Final review F1: the watchdog's daily P&L must not double count an engine
restart now that the engine restores positions/fees/bucket P&L from its state
file. The engine's own restart-safe ``daily_mtm_pnl_usd`` (per UTC day,
rewards excluded) is preferred when /status reports it."""
import json

import pytest

from mm.safety import lip_watchdog as W
from mm.status_page import status_payload
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap, trade  # noqa: F401

DAY = 86400.0


def _status(lp, start):
    rep = lp.live_snapshot(session_start_ts=start, accrual=lp.live_accrual())
    return json.loads(json.dumps(status_payload(rep)))  # the HTTP handler's encoding


def _session_one(statef):
    lp = newloop(bankroll=1500.0)
    lp.attach_state(statef)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert M in lp.resting
    lp.on_frame(trade(M, T0 + 2, "t1", 30, 5000, "no"))
    lp.on_frame(snap(M, T0 + 3, [(5, 2000)], [(90, 2000)]))  # the mark collapses
    lp.on_frame({"type": "clock", "ts": T0 + 4})
    return lp


def _restart(statef, ts):
    lp = newloop(bankroll=1500.0)
    lp.attach_state(statef)  # state file round trip
    lp.on_frame(program(M))
    lp.on_frame(snap(M, ts, [(5, 2000)], [(90, 2000)]))
    lp.on_frame({"type": "clock", "ts": ts + 1})
    return lp


def test_same_day_restart_is_not_double_counted(tmp_path):
    statef = str(tmp_path / "engine_state.json")
    cfg = W.Config({"LIP_WD_STATE_DIR": str(tmp_path / "wd")})
    state = {}
    lp = _session_one(statef)
    s1 = _status(lp, T0)
    d1 = W.daily_pnl(cfg, state, s1, T0 + 4)
    assert d1 == pytest.approx(float(s1["daily_mtm_pnl_usd"]))
    assert d1 < -30.0
    lp.save_state(force=True)

    lp2 = _restart(statef, T0 + 600)
    s2 = _status(lp2, T0 + 600)
    # the engine restored the same position: same-day MTM unchanged
    assert float(s2["daily_mtm_pnl_usd"]) == pytest.approx(d1)
    d2 = W.daily_pnl(cfg, state, s2, T0 + 601)
    assert d2 == pytest.approx(d1)  # was 2 x d1 (carry + restored markout)
    reasons, info = W.evaluate(cfg, state, T0 + 601, s2, None, T0 + 601)
    assert not any(r.startswith("daily_loss") for r in reasons), reasons
    assert info["daily_pnl_usd"] == pytest.approx(d1, abs=1e-3)


def test_restart_on_a_later_day_starts_today_at_zero(tmp_path):
    statef = str(tmp_path / "engine_state.json")
    cfg = W.Config({"LIP_WD_STATE_DIR": str(tmp_path / "wd")})
    state = {}
    lp = _session_one(statef)
    W.daily_pnl(cfg, state, _status(lp, T0), T0 + 4)
    lp.save_state(force=True)

    t = T0 + DAY + 600
    lp2 = _restart(statef, t)
    s2 = _status(lp2, t)
    assert float(s2["pnl_parts"]["markout_usd"]) < -30.0  # the loss is still held...
    assert float(s2["daily_mtm_pnl_usd"]) == pytest.approx(0.0)  # ...but it is not today's
    assert W.daily_pnl(cfg, state, s2, t + 1) == pytest.approx(0.0)


def test_legacy_state_without_pnl_day_does_not_count_history_as_today(tmp_path):
    statef = tmp_path / "engine_state.json"
    lp = _session_one(str(statef))
    lp.save_state(force=True)
    data = json.loads(statef.read_text())
    data.pop("pnl_day", None)  # state written before the day base was persisted
    statef.write_text(json.dumps(data))
    lp2 = _restart(str(statef), T0 + DAY + 600)
    assert float(lp2.daily_pnl_usd()) == pytest.approx(0.0)
    assert lp2.session_mtm_usd() < -30.0


def test_saved_state_carries_the_day_base_even_before_any_status(tmp_path):
    statef = str(tmp_path / "engine_state.json")
    lp = _session_one(statef)  # no status computed yet
    lp.save_state(force=True)
    lp2 = _restart(statef, T0 + 600)
    assert float(lp2.daily_pnl_usd()) == pytest.approx(lp.session_mtm_usd())


def test_fallback_without_engine_daily_keeps_session_baseline(tmp_path):
    cfg = W.Config({"LIP_WD_STATE_DIR": str(tmp_path)})
    state = {}
    st = {"session_elapsed_s": 60.0, "last_frame_ts": T0,
          "buckets": {"short": {"markout_usd": -20.0}}}
    assert W.daily_pnl(cfg, state, st, T0) == pytest.approx(-20.0)
    # then an engine that reports daily_mtm_pnl_usd: it is used as is
    st2 = dict(st, daily_mtm_pnl_usd="-21.5")
    st2["buckets"] = {"short": {"markout_usd": -21.5}}
    assert W.daily_pnl(cfg, state, st2, T0 + 30) == pytest.approx(-21.5)
    # and a later fallback continues from the same figure, not from 0
    st3 = {"session_elapsed_s": 120.0, "last_frame_ts": T0 + 60,
           "buckets": {"short": {"markout_usd": -22.0}}}
    assert W.daily_pnl(cfg, state, st3, T0 + 60) == pytest.approx(-22.0)
