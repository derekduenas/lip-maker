"""Review fixes: watchdog arming rule and engine/watchdog live mismatch.

Arming is decided by the watchdog's own config (LIP_WD_LIVE_ARMED and not
LIP_PAPER) plus a persisted "engine was seen live" flag. A dead engine
(status unreachable) must not disarm the cancel-all. No network.
"""
import json
import time

from mm.safety import lip_watchdog as wd


def _cfg(tmp_path, **extra):
    env = {"LIP_WD_STATE_DIR": str(tmp_path), "LIP_PAPER": "true"}
    env.update({k: str(v) for k, v in extra.items()})
    return wd.Config(env)


def _live_cfg(tmp_path, **extra):
    return _cfg(tmp_path, LIP_WD_LIVE_ARMED="true", LIP_PAPER="false",
                LIP_WD_KALSHI_KEY_ID="k", LIP_WD_KALSHI_KEY_PATH="/nonexistent", **extra)


def _status(now, **kw):
    s = {"stage": "running", "session_elapsed_s": 3600.0, "last_frame_ts": now - 5,
         "paper_capital_usd": 100.0, "resting_n": 4, "kill": None, "live_armed": False,
         "buckets": {"short": {"markout_usd": 0.0, "premium_usd": 0.0, "raw_est_usd": 0.0}},
         "markouts": {"unpaired_usd": 0.0}}
    s.update(kw)
    return s


class _Canceller:
    def __init__(self):
        self.calls = 0

    def cancel_all(self):
        self.calls += 1
        return {"ok": True, "found": 3, "remaining": 0, "errors": 0}


def _dead(cfg):
    raise ConnectionRefusedError("engine process dead")


def test_engine_dead_and_config_live_cancels(tmp_path):
    cfg = _live_cfg(tmp_path)
    c = _Canceller()
    now = time.time()
    h = wd.tick(cfg, now=now, status_fn=_dead, canceller=c)   # heartbeat missing -> trip
    assert h["latched"]
    assert c.calls == 1, "dead engine + live config must cancel-all"
    assert h["cancel"]["mode"] == "live" and h["cancel"]["ok"]
    assert h["live_armed"] is True


def test_persisted_engine_live_flag_arms_after_status_says_paper(tmp_path):
    cfg = _live_cfg(tmp_path)
    c = _Canceller()
    now = time.time()
    cfg.heartbeat.write_text(f"{now}\n")
    h = wd.tick(cfg, now=now, status_fn=lambda _c: _status(now, live_armed=True), canceller=c)
    assert not h["latched"] and c.calls == 0
    assert wd.load_state(cfg).get("engine_seen_live")
    # later status lies/flips to live_armed False, then the engine hangs
    h = wd.tick(cfg, now=now + 500, status_fn=lambda _c: _status(now + 500, live_armed=False),
                canceller=c)
    assert h["latched"] and c.calls >= 1


def test_reset_clears_persisted_flag(tmp_path):
    cfg = _live_cfg(tmp_path)
    now = time.time()
    cfg.heartbeat.write_text(f"{now}\n")
    wd.tick(cfg, now=now, status_fn=lambda _c: _status(now, live_armed=True), canceller=_Canceller())
    assert wd.load_state(cfg).get("engine_seen_live")
    wd.reset(cfg)
    assert not wd.load_state(cfg).get("engine_seen_live")


def test_reachable_paper_engine_not_cancelled_live(tmp_path):
    cfg = _live_cfg(tmp_path)
    c = _Canceller()
    now = time.time()
    # heartbeat stale but status reachable, engine says paper, never seen live
    cfg.heartbeat.write_text(f"{now - 500}\n")
    h = wd.tick(cfg, now=now, status_fn=lambda _c: _status(now), canceller=c)
    assert h["latched"] and c.calls == 0 and h["cancel"]["mode"] == "paper/noop"


def test_paper_config_never_cancels_even_if_engine_seen_live_and_dead(tmp_path):
    cfg = _cfg(tmp_path, LIP_WD_LIVE_ARMED="true")  # LIP_PAPER=true
    c = _Canceller()
    now = time.time()
    cfg.heartbeat.write_text(f"{now}\n")
    wd.tick(cfg, now=now, status_fn=lambda _c: _status(now, live_armed=True), canceller=c)
    for i in range(1, 4):
        h = wd.tick(cfg, now=now + 30 * i, status_fn=_dead, canceller=c)
    assert h["latched"] and c.calls == 0
    assert h["cancel"]["mode"] == "paper/noop"


def test_engine_live_but_watchdog_unarmed_trips_loudly(tmp_path):
    cfg = _cfg(tmp_path)  # LIP_WD_LIVE_ARMED unset, LIP_PAPER=true
    c = _Canceller()
    now = time.time()
    cfg.heartbeat.write_text(f"{now}\n")
    h = wd.tick(cfg, now=now, status_fn=lambda _c: _status(now, live_armed=True), canceller=c)
    assert h["latched"]
    assert "engine_live_watchdog_unarmed" in h["trip_reasons"]
    lines = [json.loads(x) for x in cfg.alerts_log.read_text().splitlines()]
    assert any(r["level"] == "TRIP" and "engine live but watchdog unarmed" in r["message"].lower()
               for r in lines)
    assert c.calls == 0  # still never a real API write under paper config


def test_mismatch_alerts_even_when_already_latched(tmp_path):
    cfg = _cfg(tmp_path)
    now = time.time()
    cfg.heartbeat.write_text(f"{now - 500}\n")
    h = wd.tick(cfg, now=now, status_fn=lambda _c: _status(now))
    assert h["latched"] and "engine_live_watchdog_unarmed" not in h["trip_reasons"]
    h = wd.tick(cfg, now=now + 30, status_fn=lambda _c: _status(now + 30, live_armed=True))
    lines = [json.loads(x) for x in cfg.alerts_log.read_text().splitlines()]
    assert any("engine live but watchdog unarmed" in r["message"].lower() for r in lines)
    assert "engine_live_watchdog_unarmed" in wd.load_state(cfg)["reasons"]
