"""The test suite never writes the real /var/lib/lip-maker (tests/conftest.py
points every default state path at tmp_path), the engine alert log checks the
FILE is writable, and the readiness daily-loss criterion has a lookback."""
from __future__ import annotations

import importlib.util
import os
from datetime import timedelta
from pathlib import Path

import monitor.alerts as A
from tests.test_ops_readiness_report import NOW, _run, _write_fixture

ROOT = Path(__file__).resolve().parent.parent


def test_conftest_points_state_paths_at_tmp(tmp_path):
    for name in ("LIP_ENGINE_ALERT_LOG", "LIP_SELECTION_DUMP", "LIP_RECORD_DIR", "LIP_STATE_FILE",
                 "LIP_META_CACHE", "LIP_KILL_FILE", "LIP_WD_STATE_DIR", "LIP_FV_CALIB_SAMPLES_FILE"):
        val = os.environ.get(name)
        assert val and not val.startswith("/var/lib/lip-maker"), name
    assert not str(A.alert_path()).startswith("/var/lib/lip-maker")
    assert not A.DEFAULT_ENGINE_ALERT_LOG.startswith("/var/lib/lip-maker")


def test_alert_path_falls_back_when_the_file_is_not_writable(tmp_path, monkeypatch):
    monkeypatch.delenv("LIP_ENGINE_ALERT_LOG", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    target = state / "alerts-engine.log"
    target.write_text("")
    fb = tmp_path / "repo-logs" / "alerts.log"
    monkeypatch.setattr(A, "DEFAULT_ENGINE_ALERT_LOG", str(target))
    monkeypatch.setattr(A, "FALLBACK_ALERT_LOG", str(fb))
    real = os.access
    # root ignores mode bits: simulate a root-owned 0644 file in a writable dir
    monkeypatch.setattr(A.os, "access", lambda p, mode: False if Path(p) == target else real(p, mode))
    assert A.alert_path() == fb
    A.alert("WARNING", "lip_unattended", "file not writable")
    assert "file not writable" in fb.read_text()


def test_alert_path_falls_back_when_the_path_is_a_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("LIP_ENGINE_ALERT_LOG", raising=False)
    target = tmp_path / "alerts-engine.log"
    target.mkdir()
    fb = tmp_path / "fb.log"
    monkeypatch.setattr(A, "DEFAULT_ENGINE_ALERT_LOG", str(target))
    monkeypatch.setattr(A, "FALLBACK_ALERT_LOG", str(fb))
    assert A.alert_path() == fb


def test_alert_path_uses_a_writable_existing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LIP_ENGINE_ALERT_LOG", raising=False)
    target = tmp_path / "alerts-engine.log"
    target.write_text("")
    monkeypatch.setattr(A, "DEFAULT_ENGINE_ALERT_LOG", str(target))
    assert A.alert_path() == target


def test_daily_loss_has_a_default_lookback(tmp_path):
    old = (f"{(NOW - timedelta(days=20)).isoformat()}  CRITICAL  lip_unattended  "
           "engine kill latched: daily_loss -300 <= -250")
    d = _write_fixture(tmp_path, engine_alerts=[old])
    c = _run(d)[1]["daily_loss"]
    assert c["status"] == "PASS", c
    assert c["value"]["lookback_days"] == 14
    # the window is a flag; --since still overrides it
    assert _run(d, "--daily-loss-lookback-days", "30")[1]["daily_loss"]["status"] == "FAIL"
    assert _run(d, "--since", (NOW - timedelta(days=25)).isoformat())[1]["daily_loss"]["status"] == "FAIL"
    recent = old.replace((NOW - timedelta(days=20)).isoformat(), (NOW - timedelta(days=3)).isoformat())
    assert _run(_write_fixture(tmp_path / "b", engine_alerts=[recent]))[1]["daily_loss"]["status"] == "FAIL"


def test_readiness_module_loads(tmp_path):
    spec = importlib.util.spec_from_file_location("_r2_rr", ROOT / "tools" / "readiness_report.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    assert R.parse_args([]).daily_loss_lookback_days == 14.0
