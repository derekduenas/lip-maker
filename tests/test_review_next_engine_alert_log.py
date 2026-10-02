"""Engine alerts go to /var/lib/lip-maker/alerts-engine.log (outside the code
tree deploy.sh moves), LIP_ENGINE_ALERT_LOG overrides it, and an absent or
unwritable state directory falls back without crashing."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import monitor.alerts as A
from mm.unattended import loop as L

ROOT = Path(__file__).resolve().parent.parent


def test_default_is_outside_the_code_tree():
    assert A.DEFAULT_ENGINE_ALERT_LOG == "/var/lib/lip-maker/alerts-engine.log"
    # never the watchdog's own JSON-lines log
    assert Path(A.DEFAULT_ENGINE_ALERT_LOG).name != "alerts.log"


def test_env_override(tmp_path, monkeypatch):
    dest = tmp_path / "sub" / "engine.log"
    monkeypatch.setenv("LIP_ENGINE_ALERT_LOG", str(dest))
    assert A.alert_path() == dest
    A.alert("CRITICAL", "lip_unattended", "engine kill latched: test")
    assert "engine kill latched: test" in dest.read_text()
    assert A.recent_alerts()[-1].endswith("engine kill latched: test")


def test_default_used_when_state_dir_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("LIP_ENGINE_ALERT_LOG", raising=False)
    state = tmp_path / "var-lib-lip-maker"
    state.mkdir()
    monkeypatch.setattr(A, "DEFAULT_ENGINE_ALERT_LOG", str(state / "alerts-engine.log"))
    monkeypatch.setattr(A, "FALLBACK_ALERT_LOG", str(tmp_path / "repo-logs" / "alerts.log"))
    assert A.alert_path() == state / "alerts-engine.log"
    A.alert("WARNING", "lip_unattended", "hello")
    assert "hello" in (state / "alerts-engine.log").read_text()
    assert not (tmp_path / "repo-logs").exists()


def test_falls_back_when_state_dir_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("LIP_ENGINE_ALERT_LOG", raising=False)
    monkeypatch.setattr(A, "DEFAULT_ENGINE_ALERT_LOG", str(tmp_path / "missing" / "alerts-engine.log"))
    fb = tmp_path / "repo-logs" / "alerts.log"
    monkeypatch.setattr(A, "FALLBACK_ALERT_LOG", str(fb))
    assert A.alert_path() == fb
    A.alert("WARNING", "lip_unattended", "fallback")
    assert "fallback" in fb.read_text()
    assert not (tmp_path / "missing").exists()


def test_unwritable_everything_does_not_crash(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("LIP_ENGINE_ALERT_LOG", str(blocker / "a.log"))       # parent is a file
    monkeypatch.setattr(A, "FALLBACK_ALERT_LOG", str(blocker / "b.log"))
    A.alert("CRITICAL", "lip_unattended", "nowhere to write")              # logs, no exception


def test_runloop_alert_reaches_the_engine_log(tmp_path, monkeypatch):
    dest = tmp_path / "alerts-engine.log"
    monkeypatch.setenv("LIP_ENGINE_ALERT_LOG", str(dest))
    lp = L.RunLoop(mode="paper", bankroll=5000)
    lp._alert("CRITICAL", "engine kill latched: daily_loss test")
    line = dest.read_text().strip().splitlines()[-1]
    assert "lip_unattended" in line and "daily_loss test" in line


def test_readiness_reads_the_engine_log_by_default(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("_next_rr3", ROOT / "tools" / "readiness_report.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    monkeypatch.delenv("LIP_ENGINE_ALERT_LOG", raising=False)
    logs = R.default_alert_logs()
    assert logs[:2] == ["/var/lib/lip-maker/alerts.log", "/var/lib/lip-maker/alerts-engine.log"]
    monkeypatch.setenv("LIP_ENGINE_ALERT_LOG", str(tmp_path / "e.log"))
    assert str(tmp_path / "e.log") in R.default_alert_logs()
    # the engine's text format is parsed from that file
    (tmp_path / "e.log").write_text(
        "2026-10-01T12:00:00+00:00  CRITICAL  lip_unattended  engine kill latched: daily_loss -$300\n")
    events, read, _err = R.read_alerts([str(tmp_path / "e.log")])
    assert read and events[0]["origin"] == "engine" and "daily_loss" in events[0]["message"]
