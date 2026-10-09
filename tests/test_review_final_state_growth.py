"""Final review F6: persisted state and status stay bounded.

- an ended program that earned $0 (never quoted) leaves no closed-period entry;
- closed periods of ended markets fold into per venue/bucket aggregates;
- settled positions are dropped after LIP_SETTLED_KEEP_DAYS (7) with their
  realized P&L, fees and fill counts kept in aggregates (totals unchanged);
- fills do not write the state file (covered in test_review_final_skew)."""
import json

import pytest

from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap  # noqa: F401

DAY = 86400.0


def test_never_quoted_ended_program_leaves_no_closed_period():
    lp = newloop()
    for i in range(20):
        lp.on_frame(program(f"{M}{i}", end_ts=T0 + 60))
    lp.on_frame({"type": "clock", "ts": T0})
    lp.prune_ended(T0 + 60 + 7 * 3600)
    assert not lp.programs
    assert lp.closed_periods == {} and lp.closed_periods_agg == {} and lp.closed_periods_n == 0


def _earning_loop():
    lp = newloop()
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    for i in range(1, 60):
        lp.on_frame({"type": "clock", "ts": T0 + i})
    assert M in lp.resting
    return lp


def test_closed_periods_of_ended_markets_fold_into_venue_bucket_aggregates():
    lp = _earning_loop()
    bucket = lp.bucket_of[M]
    raw = float(lp.live_accrual([M])[M]["raw_usd"])
    assert raw > 0
    lp.end_program(M)
    assert M not in lp.closed_periods
    assert lp.closed_periods_agg == {f"kalshi/{bucket}": pytest.approx(raw)}
    st = lp.live_snapshot(accrual={})
    assert st["closed_periods_n"] == 1
    assert st["closed_periods_raw_usd"] == pytest.approx(raw, abs=1e-6)
    # grok fix (a): the headline is payable (under $1 -> 0); gross is kept beside it.
    assert st["pnl_attribution"]["est_rewards_gross_kalshi_usd"] == pytest.approx(raw, abs=1e-6)
    assert st["pnl_attribution"]["est_rewards_kalshi_usd"] == 0
    assert lp.closed_periods_payable_agg == {f"kalshi/{bucket}": 0.0}
    assert st["buckets"][bucket]["raw_est_usd"] == pytest.approx(raw, abs=1e-6)


def _settled_loop(tmp_path):
    from tests.test_review_loop_pnl import trade
    lp = newloop()
    lp.attach_state(str(tmp_path / "engine_state.json"))
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(trade(M, T0 + 2, "t1", 30, 5000, "no"))
    assert lp.position[M]["yes"] > 0
    lp.on_frame({"kind": "settlement", "market": M, "result": "yes", "ts": T0 + 10})
    lp.end_program(M)  # a settled market's program is pruned; the position stays
    return lp


def _totals(lp):
    st = json.loads(json.dumps(lp.live_snapshot(accrual={}), default=str))
    b = st["buckets"]
    return (round(lp.pnl_parts()["markout_usd"], 6), round(lp.session_mtm_usd(), 6),
            {k: (round(v["markout_usd"], 6), round(v["fees_usd"], 6), v["fills_n"], round(v["premium_usd"], 6))
             for k, v in b.items()}, st["pnl_usd"])


def test_settled_positions_are_dropped_after_keep_days_with_totals_unchanged(tmp_path):
    lp = _settled_loop(tmp_path)
    before = _totals(lp)
    daily = float(lp.daily_pnl_usd())
    lp.prune_ended(T0 + 6 * DAY)
    assert M in lp.position  # kept for LIP_SETTLED_KEEP_DAYS
    lp.prune_ended(T0 + 10 + 7 * DAY + 1)
    lp.now = T0 + 10  # same loop day for the comparison below
    assert M not in lp.position and M not in lp.settled
    assert all(M not in rows for rows in lp.bucket_pos.values())
    assert _totals(lp) == before
    assert float(lp.daily_pnl_usd()) == pytest.approx(daily)
    # the aggregates survive a restart
    lp.save_state(force=True)
    saved = json.loads((tmp_path / "engine_state.json").read_text())
    assert M not in saved["position"] and M not in json.dumps(saved["bucket_pos"])
    lp2 = newloop()
    lp2.attach_state(str(tmp_path / "engine_state.json"))
    lp2.now = lp.now
    assert _totals(lp2) == before


def test_settled_keep_days_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("LIP_SETTLED_KEEP_DAYS", "1")
    lp = _settled_loop(tmp_path)
    lp.prune_ended(T0 + 10 + DAY + 1)
    assert M not in lp.position
