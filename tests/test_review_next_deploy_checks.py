"""deploy.sh runs verify_ws_frames (only with recordings) and prints the
readiness verdict after the /status check; both are informational and can
never fail the deploy. The function is extracted from deploy.sh and run
under the script's own ``set -euo pipefail`` with a stub python."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy/apex/deploy.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def test_deploy_script_parses():
    subprocess.run(["bash", "-n", str(DEPLOY)], check=True)


def _function(name: str) -> str:
    text = DEPLOY.read_text()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"{name} not found in deploy.sh"
    return m.group(0)


def test_checks_run_after_the_status_check():
    text = DEPLOY.read_text()
    status_check = text.index("NOT PAPER: stop now")
    call = text.index("\ninformational_checks\n")
    assert call > status_check


def _run(tmp_path, *, verify_rc, ready_rc, recordings, env_file_dir=None, unit_dir=None):
    app = tmp_path / "app"
    (app / ".venv/bin").mkdir(parents=True)
    (app / "tools").mkdir()
    calls = tmp_path / "calls"
    py = app / ".venv/bin/python"
    py.write_text(f"""#!/bin/bash
echo "$@" >> {calls}
case "$1" in
  *verify_ws_frames.py) echo "frame types: orderbook_delta=3"; exit {verify_rc} ;;
  *readiness_report.py) echo "[PASS        ] x"; echo "OVERALL: INSUFFICIENT DATA"; exit {ready_rc} ;;
esac
""")
    py.chmod(0o755)
    rec = tmp_path / "recordings"
    rec.mkdir()
    if recordings:
        (rec / "frames-20261001T000000Z.jsonl.gz").write_bytes(b"")
    env_file = tmp_path / "lip-maker.env"
    env_file.write_text("" if env_file_dir is None else f"LIP_RECORD_DIR={env_file_dir}\n")
    units = tmp_path / "units"
    (units / "lip-unattended.service.d").mkdir(parents=True)
    (units / "lip-unattended.service.d/policy.conf").write_text(
        "[Service]\n" + ("" if unit_dir is None else f"Environment=LIP_RECORD_DIR={unit_dir}\n"))
    script = (
        "set -euo pipefail\n"
        f"APP={app}\nENV_FILE={env_file}\nUNITS={units}\nSTATE={tmp_path}\n"
        f"DEFAULT_REC_DIR={rec}\n"
        'log() { echo "[deploy] $*"; }\n'
        + _function("informational_checks")
        + "informational_checks\necho DEPLOY-CONTINUES\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, PATH=os.environ.get("PATH", "/usr/bin:/bin")))
    called = calls.read_text().splitlines() if calls.exists() else []
    return out, called


@pytest.mark.parametrize("verify_rc,ready_rc", [(0, 0), (1, 2), (2, 1)])
def test_never_fails_the_deploy(tmp_path, verify_rc, ready_rc):
    out, called = _run(tmp_path, verify_rc=verify_rc, ready_rc=ready_rc, recordings=True)
    assert out.returncode == 0, out.stderr
    assert "DEPLOY-CONTINUES" in out.stdout
    assert any("verify_ws_frames.py" in c and "--newest 1" in c for c in called)
    assert "readiness (informational): OVERALL: INSUFFICIENT DATA" in out.stdout
    assert f"verify_ws_frames exit {verify_rc}" in out.stdout


def test_skips_verify_without_recordings(tmp_path):
    out, called = _run(tmp_path, verify_rc=0, ready_rc=2, recordings=False)
    assert out.returncode == 0 and "DEPLOY-CONTINUES" in out.stdout
    assert not any("verify_ws_frames.py" in c for c in called)
    assert "skipping verify_ws_frames" in out.stdout
    assert any("readiness_report.py" in c for c in called)


def test_record_dir_from_env_file_overrides_unit(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "frames-20261001T010000Z.jsonl.gz").write_bytes(b"")
    out, called = _run(tmp_path, verify_rc=0, ready_rc=0, recordings=False,
                       env_file_dir=str(other), unit_dir=str(tmp_path / "nope"))
    assert out.returncode == 0
    v = [c for c in called if "verify_ws_frames.py" in c]
    assert v and f"--dir {other}" in v[0]

