"""Patch 17: lip-watchdog trip logic (no network, temp dirs only)."""
import json
import time

import pytest

from mm.safety import lip_watchdog as wd


def _cfg(tmp_path, **extra):
    env = {"LIP_WD_STATE_DIR": str(tmp_path), "LIP_PAPER": "true",
           "LIP_WD_ALERT_MIN_INTERVAL_S": "600"}
    env.update({k: str(v) for k, v in extra.items()})
    return wd.Config(env)


def _status(now, **kw):
    s = {"stage": "running", "session_elapsed_s": 3600.0, "last_frame_ts": now - 5,
         "paper_capital_usd": 1000.0, "resting_n": 40, "kill": None, "live_armed": False,
         "buckets": {"durable": {"markout_usd": 0.0, "premium_usd": 10.0, "raw_est_usd": 5.0},
                     "short": {"markout_usd": -1.0, "premium_usd": 10.0, "raw_est_usd": 5.0}}}
    s.update(kw)
    return s


def _hb(cfg, ts):
    cfg.heartbeat.write_text(f"{ts}\n")


def test_healthy_no_trip(tmp_path):
    cfg = _cfg(tmp_path); now = time.time(); _hb(cfg, now - 3)
    h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now))
    assert h["ok"] and not h["latched"] and not cfg.kill_file.exists()
    assert not cfg.alerts_log.exists()


def test_stale_heartbeat_trips_and_latches(tmp_path):
    cfg = _cfg(tmp_path); now = time.time(); _hb(cfg, now - 500)
    h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now))
    assert h["latched"] and any(r.startswith("heartbeat_stale") for r in h["trip_reasons"])
    kill = json.loads(cfg.kill_file.read_text())
    assert kill["reason"].startswith("watchdog:heartbeat_stale")
    assert h["cancel"]["mode"] == "paper/noop"
    rec = json.loads(cfg.alert_json.read_text()); assert rec["level"] == "TRIP"
    # recovers -> still latched until reset
    _hb(cfg, now + 30)
    h2 = wd.tick(cfg, now=now + 31, status_fn=lambda c: _status(now + 31))
    assert h2["latched"] and h2["reasons_now"] == [] and cfg.kill_file.exists()
    wd.reset(cfg)
    assert not cfg.kill_file.exists()
    h3 = wd.tick(cfg, now=now + 62, status_fn=lambda c: _status(now + 62))
    assert h3["ok"] and not h3["latched"]


def test_missing_heartbeat_trips(tmp_path):
    cfg = _cfg(tmp_path); now = time.time()
    h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now))
    assert "heartbeat_missing" in h["trip_reasons"]


def test_status_unreachable_needs_consecutive(tmp_path):
    cfg = _cfg(tmp_path); now = time.time(); _hb(cfg, now)

    def boom(c):
        raise OSError("refused")
    assert not wd.tick(cfg, now=now, status_fn=boom)["latched"]
    _hb(cfg, now + 30)
    h = wd.tick(cfg, now=now + 30, status_fn=boom)
    assert h["latched"] and h["trip_reasons"][0].startswith("status_unreachable")


def test_feed_stale_and_grace(tmp_path):
    cfg = _cfg(tmp_path); now = time.time(); _hb(cfg, now)
    # in grace: old frame ignored
    assert not wd.tick(cfg, now=now, status_fn=lambda c: _status(now, session_elapsed_s=60, last_frame_ts=None))["latched"]
    h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now, last_frame_ts=now - 400))
    assert h["latched"] and h["trip_reasons"][0].startswith("feed_stale")


def test_daily_loss(tmp_path):
    cfg = _cfg(tmp_path, LIP_WD_DAILY_LOSS="-50"); now = time.time(); _hb(cfg, now)
    st = _status(now, session_elapsed_s=60.0)  # session started now -> baseline 0
    st["buckets"]["short"]["markout_usd"] = -20.0
    assert not wd.tick(cfg, now=now, status_fn=lambda c: st)["latched"]
    st2 = _status(now + 30, session_elapsed_s=90.0)
    st2["buckets"]["short"]["markout_usd"] = -60.0
    _hb(cfg, now + 30)
    h = wd.tick(cfg, now=now + 30, status_fn=lambda c: st2)
    assert h["latched"] and h["trip_reasons"][0].startswith("daily_loss")


def test_daily_loss_carries_across_restart(tmp_path):
    cfg = _cfg(tmp_path, LIP_WD_DAILY_LOSS="50"); now = time.time(); _hb(cfg, now)
    st = _status(now, session_elapsed_s=60.0); st["buckets"]["short"]["markout_usd"] = -30.0
    wd.tick(cfg, now=now, status_fn=lambda c: st)
    # engine restarted 2000s later; new session loses another 25 -> -55 today
    t = now + 2000; _hb(cfg, t)
    st2 = _status(t, session_elapsed_s=30.0); st2["buckets"]["short"]["markout_usd"] = -25.0
    h = wd.tick(cfg, now=t, status_fn=lambda c: st2)
    if wd.datetime.fromtimestamp(now, wd.timezone.utc).date() == wd.datetime.fromtimestamp(t, wd.timezone.utc).date():
        assert h["latched"] and h["trip_reasons"][0].startswith("daily_loss")


def test_caps(tmp_path):
    for kw, key in ((dict(paper_capital_usd=1600.0), "capital"), (dict(resting_n=500), "resting")):
        d = tmp_path / key; d.mkdir()
        cfg = _cfg(d); now = time.time(); _hb(cfg, now)
        h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now, **kw))
        assert h["latched"] and h["trip_reasons"][0].startswith(key)
    d = tmp_path / "inv"; d.mkdir()
    cfg = _cfg(d); now = time.time(); _hb(cfg, now)
    st = _status(now); st["buckets"]["short"]["premium_usd"] = 600.0
    h = wd.tick(cfg, now=now, status_fn=lambda c: st)
    assert h["trip_reasons"][0].startswith("inventory")


def test_alert_rate_limit(tmp_path):
    cfg = _cfg(tmp_path); state = {}
    assert wd.alert(cfg, state, "k", "WARN", "a", now=1000.0)
    assert not wd.alert(cfg, state, "k", "WARN", "b", now=1100.0)
    assert wd.alert(cfg, state, "k", "WARN", "c", now=1700.0)
    assert len(cfg.alerts_log.read_text().splitlines()) == 2


def test_cancel_never_live_in_paper(tmp_path):
    cfg = _cfg(tmp_path, LIP_WD_LIVE_ARMED="true")  # LIP_PAPER=true still blocks
    calls = []

    class C:
        def cancel_all(self):
            calls.append(1); return {"ok": True}
    st = {}
    res = wd.cancel_all_action(cfg, st, {"live_armed": True}, 0.0, canceller=C())
    assert res["mode"] == "paper/noop" and calls == []
    cfg2 = _cfg(tmp_path, LIP_WD_LIVE_ARMED="true", LIP_PAPER="false")
    assert wd.cancel_all_action(cfg2, st, {"live_armed": False}, 0.0, canceller=C())["mode"] == "paper/noop"
    assert calls == []


def test_live_cancel_fail_closed(tmp_path):
    cfg = _cfg(tmp_path, LIP_WD_LIVE_ARMED="true", LIP_PAPER="false")

    class Bad:
        def cancel_all(self):
            return {"found": 3, "errors": 1, "remaining": 1, "ok": False}
    st = {}
    res = wd.cancel_all_action(cfg, st, {"live_armed": True}, 0.0, canceller=Bad())
    assert res["mode"] == "live" and not res["ok"]
    assert "cancel_failed" in st["alert_last"]


def test_corrupt_state_is_latched(tmp_path):
    cfg = _cfg(tmp_path); cfg.state_file.write_text("{not json")
    now = time.time(); _hb(cfg, now)
    h = wd.tick(cfg, now=now, status_fn=lambda c: _status(now))
    assert h["latched"] and cfg.kill_file.exists()


def test_engine_honors_kill_file(tmp_path):
    from mm.unattended.service import _honor_kill_file

    class L:
        mode = "paper"; kill = None; resting = {"A": 1, "B": 1}; cancelled = []

        def external_kill(self, reason):
            from mm.unattended.loop import RunLoop
            RunLoop.external_kill(self, reason)

        def _cancel_all(self, reason):
            self.cancelled.append(reason)
    loop = L(); p = tmp_path / "KILL"
    assert _honor_kill_file(loop, str(p)) is False and loop.kill is None
    p.write_text(json.dumps({"reason": "watchdog:test"}))
    assert _honor_kill_file(loop, str(p)) is True
    assert loop.kill["reason"] == "external_kill:watchdog:test" and loop.kill["cancel_all"]
    assert loop.cancelled == ["external_kill:watchdog:test"]
    _honor_kill_file(loop, str(p))  # idempotent
    assert len(loop.cancelled) == 1
    p.write_text("garbage")  # unreadable content still kills (fail closed)
    loop2 = L(); loop2.cancelled = []
    assert _honor_kill_file(loop2, str(p)) and loop2.kill["reason"].startswith("external_kill:")
