"""Restart Flap Watcher — supervisor blind-spot closer (2026-05-13).

systemd's StartLimitBurst/OnFailure react only to FAILED units. Clean
exit-0 + Restart=always is invisible — the bug that hid the 01:04-01:52
UTC empty-universe storm (~36 silent restart cycles, no alerts.log).

Counts `Started <unit>` lines in journalctl over WINDOW_MIN minutes.
If count >= THRESHOLD_COUNT, writes a CRITICAL line to alerts.log.
Idempotent via a one-line state file. Exit-code agnostic.

Schedule (operator adds):
   */5 * * * * cd /root/lip-maker && PYTHONPATH=. venv/bin/python tools/restart_flap_watcher.py >> /root/lip-maker/logs/restart_flap.log 2>&1

Env tuneables: LIP_FLAP_THRESHOLD_COUNT, LIP_FLAP_THRESHOLD_WINDOW_MIN,
LIP_FLAP_UNIT, LIP_FLAP_STATE_FILE, LIP_FLAP_ALERTS_LOG.
"""
from __future__ import annotations
import json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

THRESHOLD_COUNT = int(os.getenv("LIP_FLAP_THRESHOLD_COUNT", "5"))
WINDOW_MIN      = int(os.getenv("LIP_FLAP_THRESHOLD_WINDOW_MIN", "10"))
UNIT            = os.getenv("LIP_FLAP_UNIT", "lip-maker.service")
STATE_FILE      = os.getenv("LIP_FLAP_STATE_FILE", "/root/lip-maker/data/restart_flap_state.json")
ALERTS_LOG      = os.getenv("LIP_FLAP_ALERTS_LOG", "/root/lip-maker/logs/alerts.log")


def _count_starts(unit: str, window_min: int) -> int:
    """Count `Started <unit>` lines from journalctl. -1 if journalctl unavailable."""
    try:
        cp = subprocess.run(
            ["journalctl", "-u", unit, "--since", f"{int(window_min)} minutes ago", "--output=cat"],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"journalctl unavailable: {e}", file=sys.stderr)
        return -1
    if cp.returncode != 0:
        print(f"journalctl rc={cp.returncode}: {cp.stderr.strip()}", file=sys.stderr)
        return -1
    short = unit.split(".")[0]
    return sum(
        1 for line in cp.stdout.splitlines()
        if line.startswith(f"Started {unit}") or (line.startswith("Started ") and short in line)
    )


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _emit_alert(now: datetime, count: int) -> None:
    line = (f"{now.isoformat()}  WATCHDOG  CRITICAL RESTART FLAP: {UNIT} started "
            f"{count} times in last {WINDOW_MIN} min — clean-exit loop possible. "
            f"Check run_paper.py exit conditions.\n")
    Path(ALERTS_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_LOG, "a") as f:
        f.write(line)
    print(line, end="")


def main() -> int:
    now = datetime.now(timezone.utc)
    count = _count_starts(UNIT, WINDOW_MIN)
    if count < 0:
        return 1
    if count < THRESHOLD_COUNT:
        print(f"OK — {UNIT} started {count} times in last {WINDOW_MIN}min "
              f"(threshold {THRESHOLD_COUNT}) at {now.isoformat()[:19]}")
        return 0
    state = _load_state()
    # Idempotency: don't double-alert while the same rolling window is in view.
    last = state.get("last_alert_iso")
    if last:
        try:
            age_min = (now - datetime.fromisoformat(last)).total_seconds() / 60.0
            if age_min < WINDOW_MIN:
                print(f"flap detected ({count} starts) but suppressed — "
                      f"already alerted {age_min:.1f}min ago (window {WINDOW_MIN}min)")
                return 0
        except ValueError:
            pass
    _emit_alert(now, count)
    state["last_alert_iso"] = now.isoformat()
    state["last_alert_count"] = count
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
