"""Review fixes: watchdog fails closed when it cannot write the kill file or
when its own tick keeps failing; corrupt state is latched and treated as
possibly-live. No real subprocess or network."""
import json
import time

from mm.safety import lip_watchdog as wd


def _cfg(tmp_path, **extra):
    env = {"LIP_WD_STATE_DIR": str(tmp_path), "LIP_PAPER": "true"}
    env.update({k: str(v) for k, v in extra.items()})
    return wd.Config(env)


class _Runner:
    def __init__(self, rc=0):
        self.cmds = []
        self.rc = rc

    def __call__(self, cmd, **kw):
        self.cmds.append(list(cmd))

        class R:
            returncode = self.rc
            stdout = ""
            stderr = "denied" if self.rc else ""
        return R()


def _unwritable_kill(tmp_path):
    blocker = tmp_path / "notadir"
    blocker.write_text("x")             # a file where a directory is needed: fails even as root
    return str(blocker / "KILL")


def test_kill_file_unwritable_stops_engine(tmp_path):
    cfg = _cfg(tmp_path, LIP_KILL_FILE=_unwritable_kill(tmp_path))
    run = _Runner()
    now = time.time()
    h = wd.tick(cfg, now=now, status_fn=lambda _c: (_ for _ in ()).throw(OSError("dead")), runner=run)
    assert h["latched"] and not h["kill_file_present"]
    assert run.cmds == [["systemctl", "stop", "lip-unattended"]]
    st = wd.load_state(cfg)
    assert st["engine_stop"]["ok"] is True
    msgs = [json.loads(x)["message"] for x in cfg.alerts_log.read_text().splitlines()]
    assert any("kill file" in m for m in msgs)


def test_stop_cmd_override_and_failure_alerts(tmp_path):
    cfg = _cfg(tmp_path, LIP_KILL_FILE=_unwritable_kill(tmp_path),
               LIP_WD_STOP_CMD="sudo -n systemctl stop lip-unattended.service")
    run = _Runner(rc=1)
    now = time.time()
    wd.tick(cfg, now=now, status_fn=lambda _c: (_ for _ in ()).throw(OSError("dead")), runner=run)
    assert run.cmds == [["sudo", "-n", "systemctl", "stop", "lip-unattended.service"]]
    st = wd.load_state(cfg)
    assert st["engine_stop"]["ok"] is False
    assert "engine_stop_failed" in st["alert_last"]


def test_tick_failures_latch_after_threshold(tmp_path):
    cfg = _cfg(tmp_path, LIP_WD_TICK_FAILS="3")
    run = _Runner()
    now = time.time()
    wd.tick_failed(cfg, 1, RuntimeError("boom"), now=now, runner=run)
    assert not wd.load_state(cfg).get("latched") and not cfg.kill_file.exists()
    wd.tick_failed(cfg, 3, RuntimeError("boom"), now=now + 60, runner=run)
    st = wd.load_state(cfg)
    assert st["latched"] and any(r.startswith("watchdog_tick_failing") for r in st["reasons"])
    assert cfg.kill_file.exists()
    lv = [json.loads(x)["level"] for x in cfg.alerts_log.read_text().splitlines()]
    assert "TRIP" in lv


def test_main_loop_counts_tick_failures(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, LIP_WD_TICK_FAILS="2")
    monkeypatch.setattr(wd, "Config", lambda env=None: cfg)

    def bad_tick(c, **kw):
        raise RuntimeError("bug in tick")
    monkeypatch.setattr(wd, "tick", bad_tick)
    seen = []
    monkeypatch.setattr(wd, "tick_failed", lambda c, n, exc, **kw: seen.append(n))
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(wd.time, "sleep", fake_sleep)
    try:
        wd.main([])
    except KeyboardInterrupt:
        pass
    assert seen == [1, 2, 3]


def test_corrupt_state_counts_as_engine_seen_live(tmp_path):
    cfg = _cfg(tmp_path, LIP_WD_LIVE_ARMED="true", LIP_PAPER="false",
               LIP_WD_KALSHI_KEY_ID="k", LIP_WD_KALSHI_KEY_PATH="/x")
    cfg.state_file.write_text("{not json")
    calls = []

    class C:
        def cancel_all(self):
            calls.append(1)
            return {"ok": True, "found": 0, "remaining": 0, "errors": 0}
    now = time.time(); cfg.heartbeat.write_text(f"{now}\n")
    st = {"session_elapsed_s": 10.0, "last_frame_ts": now, "live_armed": False}
    h = wd.tick(cfg, now=now, status_fn=lambda _c: st, canceller=C())
    assert h["latched"] and calls == [1]
