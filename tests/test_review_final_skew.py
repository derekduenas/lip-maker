"""Final review F3: the clock-skew guard pulls only on sustained skew
(LIP_CLOCK_SKEW_N consecutive skewed book frames, default 3, or skew lasting
LIP_CLOCK_SKEW_SUSTAIN_S), default limit 5 s, and re-quotes on the next
clean frame instead of waiting for the next selection. State writes (fsync)
and status file writes happen outside loop.lock, the estimate refresh is
done in bounded batches, and fills no longer fsync one by one."""
import json
import threading

import pytest

from mm.unattended import loop as L
from mm.unattended import service as S
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap, trade  # noqa: F401


def _delta(ts, lag, qty="0.00"):
    return {"type": "orderbook_delta", "ts": ts, "exchange_ts": ts - lag,
            "msg": {"market_ticker": M, "price_dollars": "0.3800", "delta_fp": qty, "side": "yes"}}


def _quoting():
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert M in lp.resting
    return lp


def test_one_late_frame_does_not_pull():
    lp = _quoting()
    lp.on_frame(_delta(T0 + 2, 3.0, "5.00"))  # 3 s late: under the 5 s default
    assert M in lp.resting
    lp.on_frame(_delta(T0 + 3, 30.0))  # one very late frame alone: no pull either
    assert M in lp.resting and lp.skew_n == 1
    lp.on_frame(_delta(T0 + 4, 0.1))  # clean: streak reset
    lp.on_frame(_delta(T0 + 5, 30.0))
    lp.on_frame(_delta(T0 + 6, 30.0))
    assert M in lp.resting


def test_n_consecutive_skewed_frames_pull_and_next_clean_frame_requotes(monkeypatch):
    # Legacy rule (LIP_CLOCK_SKEW_CLEAR_S=0): one clean frame clears.
    monkeypatch.setenv("LIP_CLOCK_SKEW_CLEAR_S", "0")
    lp = _quoting()
    for k in range(3):
        lp.on_frame(_delta(T0 + 2 + 0.1 * k, 6.0))
    assert M not in lp.resting and lp.pulls.get("clock_skew") == 1
    # still skewed: periodic selection must not re-quote into it
    lp.on_frame(_delta(T0 + 700, 6.0))
    lp.on_frame({"type": "clock", "ts": T0 + 701})
    assert M not in lp.resting
    lp.on_frame(_delta(T0 + 702, 0.2))  # first clean frame
    assert M in lp.resting  # re-quoted now, not at the next selection (+600 s)


def test_sustained_skew_pulls_before_n_frames(monkeypatch):
    monkeypatch.setenv("LIP_CLOCK_SKEW_N", "10")
    lp = _quoting()
    lp.on_frame(_delta(T0 + 2, 6.0))
    assert M in lp.resting
    lp.on_frame(_delta(T0 + 6, 6.0))  # skewed for 4 s > LIP_CLOCK_SKEW_SUSTAIN_S (3)
    assert M not in lp.resting


def test_frames_without_exchange_time_do_not_touch_the_guard():
    lp = _quoting()
    lp.on_frame(_delta(T0 + 2, 6.0))
    lp.on_frame(_delta(T0 + 2.1, 6.0))
    lp.on_frame(snap(M, T0 + 2.2, [(40, 2000)], [(55, 2000)]))  # replay/PM US style: no exchange_ts
    lp.on_frame(_delta(T0 + 2.3, 6.0))
    assert M not in lp.resting


# ------------------------------------------------------------------ lock work
def _lock_free_elsewhere(lock):
    out = []

    def probe():
        ok = lock.acquire(timeout=1.0)
        if ok:
            lock.release()
        out.append(ok)
    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return out[0]


def test_fills_do_not_fsync_and_the_timer_writes_state_outside_the_lock(tmp_path, monkeypatch):
    lp = _quoting()
    lp.attach_state(str(tmp_path / "engine_state.json"))
    fsyncs = []
    real = L.os.fsync

    def fsync(fd):
        fsyncs.append(_lock_free_elsewhere(lp.lock))
        return real(fd)
    monkeypatch.setattr(L.os, "fsync", fsync)
    lp.on_frame(trade(M, T0 + 2, "t1", 30, 5000, "no"))
    lp.on_frame(trade(M, T0 + 3, "t2", 30, 5000, "no"))
    assert lp.fills_total >= 1 and fsyncs == []  # no per-fill fsync
    timer = S.EngineTimer(lp, heartbeat=str(tmp_path / "hb"), kill_path=str(tmp_path / "KILL"))
    lp._state_saved_at = 0.0
    timer.tick()
    assert fsyncs == [True]  # one write, and the frame thread could take the lock meanwhile
    saved = json.loads((tmp_path / "engine_state.json").read_text())
    assert saved["fills_total"] == lp.fills_total
    timer.tick()
    assert fsyncs == [True]  # debounced: nothing new to write


def test_status_write_happens_outside_the_lock(tmp_path):
    lp = _quoting()
    seen = []
    ref = S.LiveStatusRefresher(lp, lambda rep: seen.append(_lock_free_elsewhere(lp.lock)),
                                data_source="x", ws_url="y", clock=lambda: 100.0)
    timer = S.EngineTimer(lp, heartbeat=str(tmp_path / "hb"), kill_path=str(tmp_path / "KILL"),
                          refresher=ref)
    timer.tick()
    assert seen == [True]


def test_estimate_refresh_is_done_in_bounded_batches(monkeypatch):
    monkeypatch.setenv("LIP_STATUS_EST_BATCH", "2")
    lp = newloop()
    for i in range(5):
        lp.on_frame(program(f"{M}{i}"))
        lp.quoted_ever.add(f"{M}{i}")
    calls = []
    real = lp.live_estimates
    monkeypatch.setattr(lp, "live_estimates", lambda ms=None: calls.append(len(list(ms))) or real(ms))
    t = [0.0]
    ref = S.LiveStatusRefresher(lp, lambda rep: None, data_source="x", ws_url="y", clock=lambda: t[0])
    for k in range(4):
        t[0] = float(k)
        ref.step()
    assert calls and max(calls) <= 2
    assert sum(calls[:3]) == 5  # one full pass over the 5 quoted markets
    assert set(ref._estimates) == {f"{M}{i}" for i in range(5)}
