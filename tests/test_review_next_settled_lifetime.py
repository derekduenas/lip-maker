"""Lifetime settled-position counters survive LIP_SETTLED_KEEP_DAYS pruning
and restarts, and the readiness report reads them."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap, trade  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_next_rr", ROOT / "tools" / "readiness_report.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

DAY = 86400.0


def _settled_loop(tmp_path, result="yes"):
    lp = newloop()
    lp.attach_state(str(tmp_path / "engine_state.json"))
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(trade(M, T0 + 2, "t1", 30, 5000, "no"))
    pos = dict(lp.position[M])
    assert pos["yes"] > 0
    lp.on_frame({"kind": "settlement", "market": M, "result": result, "ts": T0 + 10})
    lp.end_program(M)
    return lp, pos


def test_settled_counters_count_held_positions_and_survive_pruning(tmp_path):
    lp, pos = _settled_loop(tmp_path)
    expect = pos["yes"] * 1.0 - pos["yes_cost"] - pos["no_cost"] + pos["no"] * 0.0
    st = lp.live_snapshot(accrual={})
    assert st["settled_positions_n"] == 1
    assert st["settled_positions_by_venue"] == {"kalshi": 1}
    assert st["settled_total_usd"] == pytest.approx(expect, abs=1e-6)
    assert st["settled_positions_lower_bound"] is False
    # a market we never held settles: not a settled position
    lp.on_frame(program("KXOTHER-1"))
    lp.settle("KXOTHER-1", "no")
    assert lp.live_snapshot(accrual={})["settled_positions_n"] == 1
    # pruned after LIP_SETTLED_KEEP_DAYS: the row goes, the counters stay
    lp.prune_ended(T0 + 10 + 7 * DAY + 1)
    assert M not in lp.settled
    st = lp.live_snapshot(accrual={})
    assert st["settled_positions_n"] == 1 and st["settled_total_usd"] == pytest.approx(expect, abs=1e-6)
    # and a restart
    lp.save_state(force=True)
    saved = json.loads((tmp_path / "engine_state.json").read_text())
    assert saved["settled_lifetime"]["by_venue"] == {"kalshi": 1}
    lp2 = newloop()
    lp2.attach_state(str(tmp_path / "engine_state.json"))
    assert lp2.kill is None
    st2 = lp2.live_snapshot(accrual={})
    assert st2["settled_positions_n"] == 1 and st2["settled_total_usd"] == pytest.approx(expect, abs=1e-6)
    assert st2["settled_positions_lower_bound"] is False


def test_settling_twice_counts_once(tmp_path):
    lp, _pos = _settled_loop(tmp_path)
    lp.settle(M, "yes")
    assert lp.live_snapshot(accrual={})["settled_positions_n"] == 1


def test_old_state_file_seeds_a_lower_bound(tmp_path):
    lp, pos = _settled_loop(tmp_path, result="no")
    lp.realized_pruned_usd = -2.5          # an earlier pruned settlement we can no longer count
    lp.save_state(force=True)
    path = tmp_path / "engine_state.json"
    data = json.loads(path.read_text())
    data.pop("settled_lifetime")
    path.write_text(json.dumps(data))
    lp2 = newloop()
    lp2.attach_state(str(path))
    assert lp2.kill is None
    st = lp2.live_snapshot(accrual={})
    assert st["settled_positions_n"] == 1 and st["settled_positions_lower_bound"] is True
    expect = -2.5 + (pos["no"] * 1.0 - pos["yes_cost"] - pos["no_cost"])
    assert st["settled_total_usd"] == pytest.approx(expect, abs=1e-6)


def test_malformed_settled_lifetime_is_refused(tmp_path):
    lp, _pos = _settled_loop(tmp_path)
    lp.save_state(force=True)
    path = tmp_path / "engine_state.json"
    data = json.loads(path.read_text())
    data["settled_lifetime"] = {"by_venue": {"kalshi": "many"}}
    path.write_text(json.dumps(data))
    lp2 = newloop()
    lp2.attach_state(str(path))
    assert lp2.kill is not None and "state_file_unreadable" in lp2.kill["reason"]


def _args(**kw):
    a = argparse.Namespace(min_settled=100)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_readiness_reads_lifetime_counters():
    c = R.crit_settled({"settled_positions_n": 120, "settled_positions_by_venue": {"kalshi": 100, "pmus": 20},
                        "settled_total_usd": 3.5, "settled_positions_lower_bound": False}, None, _args())
    assert c["status"] == R.PASS and c["value"]["n"] == 120 and c["value"]["by_venue"]["pmus"] == 20
    c = R.crit_settled({"settled_positions_n": 40, "settled_positions_lower_bound": False}, None, _args())
    assert c["status"] == R.FAIL
    # seeded from an older state file: a short count is not a FAIL
    c = R.crit_settled({"settled_positions_n": 40, "settled_positions_lower_bound": True}, None, _args())
    assert c["status"] == R.INSUFF
    c = R.crit_settled({"settled_positions_n": 140, "settled_positions_lower_bound": True}, None, _args())
    assert c["status"] == R.PASS
    # no status: the state file's persisted counters
    state = {"settled_lifetime": {"by_venue": {"kalshi": 101}, "total_usd": 1.0, "lower_bound": False},
             "settled": {}}
    c = R.crit_settled(None, state, _args())
    assert c["status"] == R.PASS and c["value"]["n"] == 101
