"""Gap 3: queue-model sensitivity band for the paper fill simulator and the replay bench.

Default behaviour is unchanged (the existing simulator tests are untouched). The models:
  depletion   (default) trades + clip the queue to the displayed level
  risk_averse trades only: cancellations ahead are never assumed (most pessimistic)
  prob_power  trades + a book decrease at our price is attributed ahead of us with
              probability ahead^n / (ahead^n + behind^n) (hftbacktest-style)"""
import pytest

from execution.paper_fills import PaperFillSimulator
from mm import replay_bench as B
from mm.unattended import bookrec as R
from tests.test_patch19 import T0, _drain, _program, _snap, _trade
from tests.test_fill_fix_2026_10_04 import M, _Book, _tr


def _sim(model="depletion", power=1.0, queue=100, cancel_ms=0.0, latency=0):
    sim = PaperFillSimulator(latency_ms=latency, queue_model=model, queue_power=power,
                             cancel_latency_ms=cancel_ms)
    sim.track(order_id="o", market_ticker=M, side="yes", price_cents=40, size=50,
              book=_Book([(40, queue)]), now=T0)
    return sim


def test_default_model_is_depletion_and_unknown_models_are_refused():
    assert PaperFillSimulator().queue_model == "depletion"
    with pytest.raises(ValueError):
        PaperFillSimulator(queue_model="optimistic")


def test_risk_averse_ignores_cancellations_ahead_of_us():
    sim = _sim("risk_averse")
    sim.on_book_level(M, "yes", 40, 10, now=T0 + 1)          # 90 contracts vanished from the level
    assert sim.orders["o"].queue_ahead == 100
    fills = sim.apply_trades([_tr("t1", 40, 100, T0 + 2)])    # a print only eats the queue
    assert fills == [] and sim.orders["o"].queue_ahead == 0


def test_prob_power_splits_a_decrease_by_queue_position():
    sim = _sim("prob_power", power=1.0)
    sim.on_book_level(M, "yes", 40, 200, now=T0 + 1)          # 100 joined behind us
    assert sim.orders["o"].queue_ahead == 100 and sim.orders["o"].behind == 100
    sim.on_book_level(M, "yes", 40, 100, now=T0 + 2)          # 100 left: half of it was ahead
    assert sim.orders["o"].queue_ahead == pytest.approx(50) and sim.orders["o"].behind == pytest.approx(50)


def test_prob_power_exponent_makes_the_front_of_a_long_queue_more_pessimistic():
    ahead = 100
    out = {}
    for n in (1.0, 2.0, 3.0):
        sim = _sim("prob_power", power=n)
        sim.on_book_level(M, "yes", 40, 400, now=T0 + 1)      # 300 behind
        sim.on_book_level(M, "yes", 40, 300, now=T0 + 2)      # 100 left
        out[n] = sim.orders["o"].queue_ahead
    assert out[1.0] == pytest.approx(ahead - 100 * (100 / 400)) == pytest.approx(75)
    assert out[1.0] < out[2.0] < out[3.0] <= ahead            # larger n: less credit for depletion


def test_models_are_ordered_from_pessimistic_to_optimistic_on_a_cancellation_scenario():
    """100 ahead, 100 behind; 100 cancel; then a 120 print: only the optimistic models fill us."""
    got = {}
    for name, kw in (("risk_averse", {}), ("depletion", {}), ("prob_power", {"power": 1.0})):
        sim = _sim(name, **kw)
        sim.on_book_level(M, "yes", 40, 200, now=T0 + 1)
        sim.on_book_level(M, "yes", 40, 100, now=T0 + 2)
        got[name] = sum(f["count"] for f in sim.apply_trades([_tr("t", 40, 120, T0 + 30)]))
    assert got["risk_averse"] <= got["depletion"] <= got["prob_power"]
    assert got["risk_averse"] == 20 and got["prob_power"] == 50


def test_trade_then_its_book_delta_is_not_counted_twice_in_prob_power():
    sim = _sim("prob_power")
    sim.apply_trades([_tr("t1", 40, 30, T0 + 1)])              # 30 of the queue ahead printed
    assert sim.orders["o"].queue_ahead == 70
    sim.on_book_level(M, "yes", 40, 70, now=T0 + 1.1)           # the matching depth delta
    assert sim.orders["o"].queue_ahead == pytest.approx(70)


def test_cancel_latency_leaves_a_cancelled_order_exposed_for_a_moment():
    sim = _sim(cancel_ms=500, queue=0)
    sim.untrack("o", now=T0 + 10)
    assert "o" not in sim.orders
    assert [f["count"] for f in sim.apply_trades([_tr("t1", 40, 20, T0 + 10.3)])] == [20]     # picked off
    assert sim.apply_trades([_tr("t2", 40, 20, T0 + 10.7)]) == []                           # cancel landed


def test_no_cancel_latency_by_default_and_a_replacement_is_not_confused_with_the_old_order():
    sim = _sim(queue=0)
    sim.untrack("o", now=T0 + 10)
    assert sim.apply_trades([_tr("t1", 40, 20, T0 + 10.1)]) == []
    sim = _sim(cancel_ms=500, queue=0)
    sim.untrack("o", now=T0 + 10)
    sim.track(order_id="o", market_ticker=M, side="yes", price_cents=41, size=50,
              book=_Book([]), now=T0 + 10)                      # re-quoted at a new price
    fills = sim.apply_trades([_tr("t1", 40, 20, T0 + 10.3)])    # a 40c print: through the new 41c bid, and
    assert sorted(f["price_cents"] for f in fills) == [40, 41]   # the old 40c order is still exposed
    assert sim.orders["o"].price_cents == 41 and sim.orders["o"].remaining == 30   # replacement intact


def test_loop_reads_the_queue_model_from_the_environment(monkeypatch):
    from tests.test_review_loop_pnl import newloop
    monkeypatch.setenv("LIP_SIM_QUEUE_MODEL", "prob_power")
    monkeypatch.setenv("LIP_SIM_QUEUE_POWER", "2")
    monkeypatch.setenv("LIP_SIM_CANCEL_LATENCY_MS", "300")
    lp = newloop(bankroll=1500.0)
    assert (lp.sim.queue_model, lp.sim.queue_power, lp.sim.cancel_latency_sec) == ("prob_power", 2.0, 0.3)
    monkeypatch.setenv("LIP_SIM_QUEUE_MODEL", "typo")
    assert newloop(bankroll=1500.0).sim.queue_model == "depletion"      # a typo never stops the service


# ------------------------------------------------------------- bench band
def test_queue_band_configs_cover_the_models():
    names = [n for n, _e in B.queue_band_configs()]
    assert names[0] == "q_risk_averse" and "q_depletion" in names and "q_prob_n1" in names and "q_prob_n3" in names
    envs = dict(B.queue_band_configs())
    assert envs["q_prob_n2"] == {"LIP_SIM_QUEUE_MODEL": "prob_power", "LIP_SIM_QUEUE_POWER": "2"}


def test_replay_bench_band_spans_zero_to_one_fill_on_a_depletion_scenario(tmp_path):
    rec = R.FrameRecorder(str(tmp_path), flush_s=0, min_free_gb=0).start()
    rec.record(_program())
    frames = [_snap(T0 + 1, [(40, 3000)], [(55, 3000)])]
    for i in range(2, 300, 5):
        frames.append(_snap(T0 + i, [(40, 3000)], [(55, 3000)]))
    frames.append(_snap(T0 + 301, [(40, 1000)], [(55, 3000)]))     # 2,000 ahead of us cancelled
    frames.append(_trade(T0 + 305, "t1", 39, 1500, "no"))            # a 1,500 print through 40c
    frames.append(_snap(T0 + 306, [(39, 3000)], [(55, 3000)]))
    for f in frames:
        rec.record(f)
    _drain(rec, len(frames) + 1)
    rec.stop()
    paths = R.list_files(tmp_path)
    env = {"LIP_SIZE_LADDER": "100", "LIP_HOLDING_MODEL": "carry"}
    fills = {}
    for name, cfg in B.queue_band_configs():
        r = B.run_one(paths, dict(env, **cfg), bankroll=5000, select_every=600, warmup_s=0)
        fills[name] = r["fills"]
    band = B.band(fills)
    assert fills["q_risk_averse"] <= fills["q_depletion"] <= fills["q_prob_n1"]
    assert band == {"min": min(fills.values()), "max": max(fills.values())}
