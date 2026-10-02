"""Importing the entrypoint module must not start the service."""
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_importing_unattended_main_does_not_run_the_service(monkeypatch):
    import mm.unattended.service as service
    called = []
    monkeypatch.setattr(service, "main", lambda *a, **k: called.append(1) or 0)
    sys.modules.pop("mm.unattended.__main__", None)
    importlib.import_module("mm.unattended.__main__")
    assert called == []


def test_python_dash_m_still_runs_main(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "mm.unattended", "--help"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "--reset-kill" in proc.stdout
