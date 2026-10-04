"""Adversarial-review fixes for the engine changes (offset-aware skew, dead-man health, restore,
state growth, queue models). Each reproduced against 9ac4c20 first."""
import json

import pytest

from execution.paper_fills import PaperFillSimulator
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap  # noqa: F401
from tests.test_skew_offset_aware import _delta, _feed, _quoting


# ------------------------------------------------------------ offset-aware skew
def test_a_long_feed_gap_restarts_the_baseline_so_real_lag_after_it_still_trips(monkeypatch):
    lp = _quoting(monkeypatch, aware=True)
    ts = _feed(lp, T0 + 2, 1500, lag=0.2)
    ts += 3700.0                                         # the feed goes silent for over an hour
    for k in range(5):
        lp.on_frame(_delta(ts + 0.2 * k, 12.0))          # then real lag of 12 s
    assert lp._skew_active


def test_a_sustained_real_latency_problem_is_not_absorbed_forever(monkeypatch):
    lp = _quoting(monkeypatch, aware=True)
    ts = _feed(lp, T0 + 2, 1500, lag=0.2)
    ts = _feed(lp, ts, 3 * 3600, lag=12.0, step=5.0)     # three hours at 12 s
    assert lp._skew_active                               # the absorbed offset is capped
    assert lp.lag_report()["offset_s"] <= 5.0 + 1e-9


def test_a_downward_step_in_the_lag_floor_raises_the_shift_alert_once(monkeypatch):
    lp = _quoting(monkeypatch, aware=True)
    monkeypatch.setenv("LIP_SKEW_SHIFT_S", "2")
    ts = _feed(lp, T0 + 2, 1500, lag=4.0)
    assert lp.offset_shift_n == 0
    _feed(lp, ts, 150, lag=0.2)                          # the floor drops by 3.8 s
    assert lp.offset_shift_n == 1                        # the baseline follows a drop at once; the alert is the record
    assert lp.lag_report()["offset_s"] == pytest.approx(0.2, abs=0.01)


# ---------------------------------------------------------------- dead-man health
def test_deadman_is_unhealthy_until_a_frame_has_arrived():
    lp = newloop(bankroll=1500.0)
    assert lp.healthy_for_deadman() == (False, "no_frames")


# ------------------------------------------------------------ restore robustness
def _state_file(monkeypatch, tmp_path, mutate):
    lp = newloop(bankroll=1500.0)
    lp.attach_state(str(tmp_path / "s.json"))
    lp._as_note(M, 10, -2.0, T0)
    lp.mk_ewma[M] = [1.0, 2.0, 3]
    lp.save_state(force=True)
    raw = (tmp_path / "s.json").read_text()
    data = json.loads(raw)
    mutate(data)
    (tmp_path / "s.json").write_text(json.dumps(data).replace('"__INF__"', "1e400").replace('"__NAN__"', "NaN"))
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "s.json"))
    return lp2


@pytest.mark.parametrize("bad", ["__INF__", "__NAN__", "x", None, [1]])
def test_a_corrupt_guard_or_exit_value_never_latches_the_kill(monkeypatch, tmp_path, bad):
    def mutate(d):
        d["mk_ewma"] = {M: [bad, 1.0, bad]}
        d["comp_ewma"] = {M: bad}
        d["inv_since"] = {M: bad}
        d["price_event_acc"] = {"b": {"E": [bad, 1.0, 1.0]}}
    lp2 = _state_file(monkeypatch, tmp_path, mutate)
    assert lp2.kill is None and lp2.state_error is None
    import math
    assert all(math.isfinite(x) for v in lp2.mk_ewma.values() for x in v) and \
        all(math.isfinite(v) for v in lp2.comp_ewma.values()) and all(math.isfinite(v) for v in lp2.inv_since.values())


def test_a_non_finite_period_estimate_does_not_break_the_status_reconciliation():
    lp = newloop(bankroll=1500.0)
    lp.period_estimates.append({"market": "A-X", "program_id": "p", "series": "A", "estimated_usd": "NaN",
                                "period_start": "2026-09-01T00:00:00Z"})
    lp.period_estimates.append({"market": "B-X", "program_id": "q", "series": "B", "estimated_usd": "Infinity"})
    assert lp.rewards_reconciliation()["matched"] == 0
    assert lp.series_gate_report()["go_no_go"]["verdict"]["verdict"]


# ------------------------------------------------------------------ state growth
def test_the_markout_ewma_is_not_updated_or_persisted_when_the_guard_is_off(monkeypatch):
    monkeypatch.delenv("LIP_AS_GUARD_ENABLE", raising=False)
    lp = newloop(bankroll=1500.0)
    lp._as_note(M, 10, -2.0, T0)
    assert lp.mk_ewma == {}


def test_the_markout_ewma_evicts_the_least_recently_used_market(monkeypatch):
    monkeypatch.setenv("LIP_AS_GUARD_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    for i in range(2000):
        lp._as_note(f"K-{i}", 1, 1.0, T0)
    lp._as_note("K-0", 1, 1.0, T0)                       # touch the oldest: now the most recent
    lp._as_note("K-new", 1, 1.0, T0)
    assert "K-0" in lp.mk_ewma and "K-1" not in lp.mk_ewma and len(lp.mk_ewma) == 2000


def test_inventory_age_entries_are_pruned_with_their_market(monkeypatch):
    lp = newloop(bankroll=1500.0)
    lp.inv_since[M] = T0
    lp.inv_since["GONE"] = T0
    lp.position[M] = {"yes": 5.0, "no": 0.0, "yes_cost": 2.0, "no_cost": 0.0, "fees": 0.0, "venue": "kalshi"}
    lp.prune_ended(T0 + 10)
    assert M in lp.inv_since and "GONE" not in lp.inv_since           # no position: dropped
    lp.settled[M] = {"result": "yes", "ts": T0, "source": "x"}
    lp.prune_ended(T0 + 10 + 8 * 86400)
    assert M not in lp.inv_since


# --------------------------------------------------------------- queue models
def _sim(**kw):
    return PaperFillSimulator(latency_ms=0, **kw)


def _trade(tid, qty, ts="2026-10-04T12:00:10Z", ticker="T", yes_cents=40):
    return {"trade_id": tid, "ticker": ticker, "count": qty, "created_time": ts, "taker_side": "no",
            "yes_price": yes_cents, "no_price": 100 - yes_cents}


def test_a_cancelling_order_and_its_replacement_share_one_print():
    sim = _sim(cancel_latency_ms=5000)
    sim.track(order_id="a", market_ticker="T", side="yes", price_cents=40, size=30, book=None, now=0.0)
    sim.untrack("a", now=1791115200.0 - 1.0)                # cancel requested; still fillable for 5 s
    sim.track(order_id="b", market_ticker="T", side="yes", price_cents=40, size=30, book=None, now=0.0)
    fills = sim.apply_trades([_trade("t1", 30, ts="2026-10-04T12:00:00Z")])
    assert sum(f["count"] for f in fills) <= 30 + 1e-9


def test_prob_power_overflow_is_charged_to_the_front_and_never_exceeds_the_displayed_level():
    sim = _sim(queue_model="prob_power", queue_power=3.0)
    o = sim.track(order_id="a", market_ticker="T", side="yes", price_cents=40, size=10, book=None, now=0.0)
    o.queue_ahead, o.behind, o.last_level = 49.3, 1000.0, 1049.3
    sim.on_book_level("T", "yes", 40, 0.0, now=1.0)
    assert o.queue_ahead <= 1e-9


def test_queue_model_docs_do_not_claim_a_monotone_order():
    from execution import paper_fills
    assert "larger n credits less depletion" not in paper_fills.PaperFillSimulator.__init__.__doc__
    import inspect
    from mm import replay_bench
    assert "pessimistic -> optimistic" not in inspect.getsource(replay_bench.queue_band_configs)
