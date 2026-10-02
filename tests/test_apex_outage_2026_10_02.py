"""2026-10-02 APEX outage: heartbeat_missing false trip + transient auto-recover.

Root cause: write_heartbeat truncated then wrote the file; the watchdog read
it in between, got "", and latched heartbeat_missing (engine alive, feed 6.8 s
fresh, /status up). Next check 30 s later was clean, but the latch held the
engine at 0 quotes for ~7 h until a manual reset.
"""
import json
import logging
import threading
import time
from types import SimpleNamespace

from mm.safety import lip_watchdog as wd
from mm.safety.supervisor import write_heartbeat


def _cfg(tmp_path, **extra):
    env = {"LIP_WD_STATE_DIR": str(tmp_path), "LIP_PAPER": "true",
           "LIP_WD_ALERT_MIN_INTERVAL_S": "600"}
    env.update({k: str(v) for k, v in extra.items()})
    return wd.Config(env)


def _status(now, **kw):
    s = {"stage": "running", "session_elapsed_s": 3600.0, "last_frame_ts": now - 5,
         "paper_capital_usd": 400.0, "resting_n": 13, "kill": None, "live_armed": False,
         "buckets": {"durable": {"markout_usd": 0.0, "premium_usd": 0.0, "raw_est_usd": 0.0}}}
    s.update(kw)
    return s


class Runner:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self.rc, stderr="")


def _auto(tmp_path, **extra):
    base = {"LIP_WD_AUTO_RECOVER": "1", "LIP_WD_AUTO_RECOVER_HEALTHY_S": "300",
            "LIP_WD_AUTO_RECOVER_MAX_PER_DAY": "3", "LIP_WD_AUTO_RESTART_AFTER_S": "600",
            "LIP_WD_AUTO_RECOVER_GRACE_S": "180"}
    base.update(extra)
    return _cfg(tmp_path, **base)


def _tick(cfg, now, runner, hb_age=1.0, **st):
    write_heartbeat(cfg.heartbeat, now=now - hb_age)
    return wd.tick(cfg, now=now, status_fn=lambda c: _status(now, **st), runner=runner)


# ---------------------------------------------------------------- root cause
def test_write_heartbeat_is_atomic_under_concurrent_reads(tmp_path):
    d = tmp_path / "hb"
    p = d / "heartbeat"
    write_heartbeat(p)
    stop = time.time() + 1.0
    bad = []

    def writer():
        while time.time() < stop:
            write_heartbeat(p)

    t = threading.Thread(target=writer)
    t.start()
    n = 0
    while time.time() < stop:
        n += 1
        txt = p.read_text()
        try:
            float(txt.strip().split()[0])
        except Exception:
            bad.append(txt)
    t.join()
    assert n > 100 and bad == []
    assert sorted(x.name for x in d.iterdir()) == ["heartbeat"]  # no temp files left


def test_empty_heartbeat_file_is_not_missing(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.heartbeat.write_text("")          # writer caught between truncate and write
    ts = wd.read_heartbeat(cfg, pause_s=0)
    assert ts is not None and abs(ts - time.time()) < 60
    now = time.time()
    h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now))
    assert h["ok"] and "heartbeat_missing" not in h["reasons_now"]


def test_absent_heartbeat_is_still_missing(tmp_path):
    cfg = _cfg(tmp_path)
    assert wd.read_heartbeat(cfg, pause_s=0) is None
    now = time.time()
    h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now))
    assert "heartbeat_missing" in h["trip_reasons"]


def test_old_garbage_heartbeat_still_goes_stale(tmp_path):
    import os
    cfg = _cfg(tmp_path)
    cfg.heartbeat.write_text("garbage")
    old = time.time() - 1000
    os.utime(cfg.heartbeat, (old, old))
    now = time.time()
    h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now))
    assert any(r.startswith("heartbeat_stale") for r in h["trip_reasons"])


# ---------------------------------------------------------------- auto-recover
def test_transient_trip_auto_recovers_after_healthy_window(tmp_path):
    cfg = _auto(tmp_path); r = Runner(); t0 = time.time()
    cfg.heartbeat.unlink(missing_ok=True)
    h = wd.tick(cfg, now=t0, status_fn=lambda c: _status(t0), runner=r)   # the 07:17 trip
    assert h["latched"] and h["trip_reasons"] == ["heartbeat_missing"] and cfg.kill_file.exists()
    assert h["auto_recover"]["eligible"]
    h = _tick(cfg, t0 + 30, r)                       # healthy again; window starts
    assert h["latched"] and r.calls == []
    h = _tick(cfg, t0 + 300, r)                      # 270 s healthy: not yet
    assert h["latched"] and r.calls == []
    h = _tick(cfg, t0 + 331, r)                      # 301 s healthy: recover
    assert r.calls == [["systemctl", "restart", "lip-unattended"]]
    assert not h["latched"] and h["ok"] and not cfg.kill_file.exists()
    assert h["auto_recover"]["action"] == "recovered" and h["auto_recover"]["used"] == 1
    st = json.loads(cfg.state_file.read_text())
    assert st["reset_by"] == "auto_recover" and st["auto_recover"]["last_recover"]["prev_reasons"] == ["heartbeat_missing"]
    log = [json.loads(x) for x in cfg.alerts_log.read_text().splitlines()]
    assert [x["key"] for x in log][-1] == "auto_recover"
    # grace: a stale heartbeat right after the restart does not re-trip
    h = _tick(cfg, t0 + 361, r, hb_age=200)
    assert not h["latched"] and h["info"]["auto_grace_ignored"]
    # after grace it trips again normally
    h = _tick(cfg, t0 + 331 + 181, r, hb_age=200)
    assert h["latched"]


def test_real_trips_never_auto_recover(tmp_path):
    r = Runner(); t0 = time.time()
    for i, st in enumerate(({"paper_capital_usd": 5000.0}, {"resting_n": 999})):
        d = tmp_path / f"case{i}"; d.mkdir()
        cfg = _auto(d)
        h = _tick(cfg, t0, r, **st)
        assert h["latched"] and not h["auto_recover"]["eligible"]
        for k in range(1, 40):
            h = _tick(cfg, t0 + 30 * k, r)
        assert h["latched"] and cfg.kill_file.exists()
    assert r.calls == []


def test_transient_then_real_reason_while_latched_blocks_recovery(tmp_path):
    cfg = _auto(tmp_path); r = Runner(); t0 = time.time()
    cfg.heartbeat.unlink(missing_ok=True)
    wd.tick(cfg, now=t0, status_fn=lambda c: _status(t0), runner=r)
    h = _tick(cfg, t0 + 30, r, resting_n=999)        # real breach seen while latched
    assert not h["auto_recover"]["eligible"]
    for k in range(2, 40):
        h = _tick(cfg, t0 + 30 * k, r)
    assert h["latched"] and r.calls == []


def test_daily_budget_then_stays_latched(tmp_path):
    cfg = _auto(tmp_path, LIP_WD_AUTO_RECOVER_MAX_PER_DAY="2", LIP_WD_AUTO_RECOVER_GRACE_S="0")
    r = Runner()
    t = 1790899200.0 + 3600   # 2026-10-02 01:00 UTC
    for i in range(3):
        cfg.heartbeat.unlink(missing_ok=True)
        h = wd.tick(cfg, now=t, status_fn=lambda c, t=t: _status(t), runner=r)
        assert h["latched"]
        for k in range(1, 13):
            h = _tick(cfg, t + 30 * k, r)
        t += 1000
        if i < 2:
            assert not h["latched"]
        else:
            assert h["latched"] and cfg.kill_file.exists()
    assert len(r.calls) == 2
    # next UTC day: budget resets
    t = 1790899200.0 + 86400 + 60
    for k in range(0, 13):
        h = _tick(cfg, t + 30 * k, r)
    assert not h["latched"] and len(r.calls) == 3


def test_restart_failure_restores_kill_file_and_stays_latched(tmp_path):
    cfg = _auto(tmp_path); r = Runner(rc=1); t0 = time.time()
    cfg.heartbeat.unlink(missing_ok=True)
    wd.tick(cfg, now=t0, status_fn=lambda c: _status(t0), runner=r)
    for k in range(1, 13):
        h = _tick(cfg, t0 + 30 * k, r)
    assert r.calls and h["latched"] and cfg.kill_file.exists()


def test_never_auto_recovers_when_watchdog_armed_live(tmp_path):
    cfg = _auto(tmp_path, LIP_WD_LIVE_ARMED="true", LIP_PAPER="false"); r = Runner(); t0 = time.time()
    cfg.heartbeat.unlink(missing_ok=True)
    wd.tick(cfg, now=t0, status_fn=lambda c: _status(t0), runner=r, canceller=lambda: None)
    for k in range(1, 20):
        write_heartbeat(cfg.heartbeat, now=t0 + 30 * k)
        h = wd.tick(cfg, now=t0 + 30 * k, status_fn=lambda c, k=k: _status(t0 + 30 * k), runner=r,
                    canceller=SimpleNamespace(cancel_all=lambda: {"ok": True, "found": 0}))
    assert h["latched"] and r.calls == []


def test_hung_engine_gets_one_restart(tmp_path):
    cfg = _auto(tmp_path); r = Runner(); t0 = time.time()
    write_heartbeat(cfg.heartbeat, now=t0 - 500)       # stale and stays stale
    for k in range(0, 40):
        h = wd.tick(cfg, now=t0 + 30 * k, status_fn=lambda c, k=k: _status(t0 + 30 * k), runner=r)
    assert h["latched"] and r.calls == [["systemctl", "restart", "lip-unattended"]]
    assert h["auto_recover"]["used"] == 1


def test_auto_recover_off_by_default(tmp_path):
    cfg = _cfg(tmp_path); r = Runner(); t0 = time.time()
    assert not cfg.auto_recover
    cfg.heartbeat.unlink(missing_ok=True)
    wd.tick(cfg, now=t0, status_fn=lambda c: _status(t0), runner=r)
    for k in range(1, 40):
        h = _tick(cfg, t0 + 30 * k, r)
    assert h["latched"] and r.calls == []


def test_manual_reset_keeps_daily_budget(tmp_path):
    cfg = _auto(tmp_path); r = Runner(); t0 = time.time()
    cfg.heartbeat.unlink(missing_ok=True)
    wd.tick(cfg, now=t0, status_fn=lambda c: _status(t0), runner=r)
    for k in range(1, 13):
        _tick(cfg, t0 + 30 * k, r)
    wd.reset(cfg)
    assert json.loads(cfg.state_file.read_text())["auto_recover"]["used"] == 1


# ---------------------------------------------------------------- log rotation
def test_log_rotation_is_configurable(tmp_path, monkeypatch):
    from logging.handlers import RotatingFileHandler
    from mm import ops
    monkeypatch.setenv("LIP_LOG_MAX_MB", "7")
    monkeypatch.setenv("LIP_LOG_BACKUPS", "4")
    log = logging.getLogger("lip")
    before = list(log.handlers)
    try:
        ops.configure_logging(str(tmp_path / "lip.log"))
        h = [x for x in log.handlers if x not in before][0]
        assert isinstance(h, RotatingFileHandler) and h.maxBytes == 7_000_000 and h.backupCount == 4
    finally:
        for x in list(log.handlers):
            if x not in before:
                log.removeHandler(x); x.close()


def test_log_rotation_default_holds_more_history(tmp_path, monkeypatch):
    from mm import ops
    monkeypatch.delenv("LIP_LOG_MAX_MB", raising=False)
    monkeypatch.delenv("LIP_LOG_BACKUPS", raising=False)
    log = logging.getLogger("lip")
    before = list(log.handlers)
    try:
        ops.configure_logging(str(tmp_path / "lip.log"))
        h = [x for x in log.handlers if x not in before][0]
        assert h.maxBytes * (h.backupCount + 1) >= 100_000_000
    finally:
        for x in list(log.handlers):
            if x not in before:
                log.removeHandler(x); x.close()
