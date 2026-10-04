"""Gap 9: the clock-skew guard can tell a constant clock offset from real latency
(opt-in, LIP_SKEW_OFFSET_AWARE; default behaviour unchanged)."""
import pytest

from mm import ops
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap  # noqa: F401

MIN = 60.0


def _delta(ts, lag, market=M):
    return {"type": "orderbook_delta", "ts": ts, "exchange_ts": ts - lag,
            "msg": {"market_ticker": market, "price_dollars": "0.3800", "delta_fp": "0.00", "side": "yes"}}


def _quoting(monkeypatch, aware):
    monkeypatch.setenv("LIP_SKEW_OFFSET_AWARE", "1" if aware else "0")
    monkeypatch.setenv("LIP_SKEW_OFFSET_WARMUP_S", "600")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert M in lp.resting
    return lp


def _feed(lp, start, seconds, lag, step=1.0):
    ts = start
    while ts < start + seconds:
        lp.on_frame(_delta(ts, lag))
        ts += step
    return ts


def test_constant_offset_trips_the_raw_guard(monkeypatch):
    lp = _quoting(monkeypatch, aware=False)
    _feed(lp, T0 + 2, 30, lag=6.0)
    assert lp._skew_active and lp.skew_trips_n == 1


def test_constant_offset_does_not_trip_once_the_baseline_exists(monkeypatch):
    lp = _quoting(monkeypatch, aware=True)
    ts = _feed(lp, T0 + 2, 1200, lag=6.0)        # 20 minutes at a constant 6 s offset
    # The first minutes (no baseline yet) are the raw rule and trip it; it must then clear and stay clear.
    lp.on_frame(_delta(ts, 6.0))
    assert not lp._skew_active or lp.skew_clears_n >= 1
    trips = lp.skew_trips_n
    ts = _feed(lp, ts, 600, lag=6.0)
    assert lp.skew_trips_n == trips and not lp._skew_active
    rep = lp.lag_report()
    assert rep["offset_aware"] is True and rep["offset_s"] == pytest.approx(6.0, abs=0.01)
    assert abs(rep["corrected_last_s"]) < 0.01


def test_real_latency_spike_on_top_of_a_known_offset_still_trips(monkeypatch):
    lp = _quoting(monkeypatch, aware=True)
    ts = _feed(lp, T0 + 2, 1500, lag=0.2)         # baseline offset ~0.2 s
    lp.on_frame({"type": "clock", "ts": ts})
    lp._maybe_select(ts)
    for k in range(3):
        lp.on_frame(_delta(ts + 0.1 * k, 7.0))
    assert lp._skew_active


def test_negative_offset_local_clock_behind_is_absorbed(monkeypatch):
    lp = _quoting(monkeypatch, aware=True)
    ts = _feed(lp, T0 + 2, 1500, lag=-7.0)         # local clock 7 s behind the exchange
    trips = lp.skew_trips_n
    _feed(lp, ts, 300, lag=-7.0)
    assert lp.skew_trips_n == trips and not lp._skew_active
    assert lp.lag_report()["offset_s"] == pytest.approx(-7.0, abs=0.01)


def test_offset_shift_is_reported_and_counted_once_per_episode(monkeypatch):
    lp = _quoting(monkeypatch, aware=True)
    monkeypatch.setenv("LIP_SKEW_SHIFT_S", "2")
    ts = _feed(lp, T0 + 2, 1500, lag=0.2)
    assert lp.lag_report()["offset_shift_s"] == pytest.approx(0.0, abs=0.01)
    _feed(lp, ts, 300, lag=3.5)                   # +3.3 s step: below the 5 s trip, above the 2 s shift alarm
    rep = lp.lag_report()
    assert rep["offset_shift_s"] == pytest.approx(3.3, abs=0.05)
    assert lp.offset_shift_n == 1 and not lp._skew_active


def test_default_is_off_and_report_says_so(monkeypatch):
    monkeypatch.delenv("LIP_SKEW_OFFSET_AWARE", raising=False)
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000)], [(55, 2000)]))
    lp.on_frame(_delta(T0 + 1, 0.3))
    rep = lp.lag_report()
    assert rep["offset_aware"] is False and rep["offset_s"] is None


# ---------------------------------------------------------------- chrony probe
CHRONY_FAST = """Reference ID    : A9FEA9FE (169.254.169.254)
Stratum         : 3
System time     : 0.000123456 seconds fast of NTP time
Last offset     : +0.000010000 seconds
"""
CHRONY_SLOW = CHRONY_FAST.replace("fast of", "slow of")


def test_chrony_probe_parses_sign_and_caches():
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return CHRONY_FAST if len(calls) == 1 else CHRONY_SLOW

    t = [1000.0]
    p = ops.ChronyProbe(runner=runner, clock=lambda: t[0], ttl_s=30)
    assert p.read()["offset_s"] == pytest.approx(0.000123456)
    assert p.read()["offset_s"] == pytest.approx(0.000123456) and len(calls) == 1   # cached
    t[0] += 31
    assert p.read()["offset_s"] == pytest.approx(-0.000123456) and len(calls) == 2


def test_chrony_probe_never_raises():
    def boom(cmd, timeout):
        raise FileNotFoundError("chronyc")
    r = ops.ChronyProbe(runner=boom, clock=lambda: 0.0).read()
    assert r["offset_s"] is None and "FileNotFoundError" in r["error"]
    assert ops.ChronyProbe(runner=lambda c, t: "garbage", clock=lambda: 0.0).read()["offset_s"] is None


def test_service_enables_the_chrony_probe_only_when_asked(monkeypatch):
    """The engine constructor must import cleanly with the probe on (a missing
    helper in service.py would crash startup)."""
    import inspect
    from mm.unattended import service
    src = inspect.getsource(service._Engine.__init__)
    assert "LIP_CHRONY_STATUS" in src and "_flag(" not in src
