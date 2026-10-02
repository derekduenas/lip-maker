"""Suite-wide isolation from the real engine state directory.

Every default path under /var/lib/lip-maker (engine alert log, selection
dump, frame recordings, engine state, metadata cache, kill file, watchdog
state dir, calibration samples) is pointed at the test's tmp_path, so a test
that forgets to set one writes there instead of the droplet's (or this
box's) live files: the readiness report reads that alert log, and a test's
"engine kill latched: daily_loss" line there would read as a real event.

A session check also fails the run if /var/lib/lip-maker changed while the
suite ran (when that directory exists)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

VAR = Path("/var/lib/lip-maker")

STATE_ENVS = {
    "LIP_ENGINE_ALERT_LOG": "alerts-engine.log",
    "LIP_SELECTION_DUMP": "last_selection.json",
    "LIP_RECORD_DIR": "recordings",
    "LIP_STATE_FILE": "engine_state.json",
    "LIP_META_CACHE": "exchange_index_cache.json",
    "LIP_KILL_FILE": "KILL",
    "LIP_WD_STATE_DIR": "wd",
    "LIP_FV_CALIB_SAMPLES_FILE": "fv_calib_samples.jsonl",
}


def _listing(path: Path) -> dict:
    out = {}
    if not path.is_dir():
        return out
    for p in sorted(path.rglob("*")):
        try:
            st = p.stat()
        except OSError:
            continue
        out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


@pytest.fixture(scope="session", autouse=True)
def _var_lib_untouched():
    before = _listing(VAR)
    yield
    after = _listing(VAR)
    if after != before:
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        pytest.fail(f"the test suite modified {VAR}: {changed}", pytrace=False)


@pytest.fixture(autouse=True)
def _state_paths_in_tmp(tmp_path, monkeypatch):
    base = tmp_path / "_lip_state"
    base.mkdir(exist_ok=True)
    for name, leaf in STATE_ENVS.items():
        monkeypatch.setenv(name, str(base / leaf))
    # Tests that delete LIP_ENGINE_ALERT_LOG to exercise the default still
    # must not reach the real directory.
    import monitor.alerts as alerts
    monkeypatch.setattr(alerts, "DEFAULT_ENGINE_ALERT_LOG", str(base / "default-alerts-engine.log"))
    yield
