"""Sovereign event scheduler — fires the prep pipeline at T-minus milestones.

Designed to run as a persistent systemd service or hourly cron. On each tick:
  1. Check every event on the calendar
  2. Determine which "milestone" (T-72h / T-24h / T-2h / T+24h) is active
  3. If the milestone hasn't been executed for that event, run it
  4. Log milestone execution to scheduler_log (prevents duplicates)

Milestones:
  T-72h: initial thesis build (heuristic scorer, log JSON for user review)
  T-24h: re-run thesis with Claude LLM scorer, update positions
  T-2h:  execute orders (PAPER by default, live if SOV_LIVE=true + approval)
  T+24h: post-event review, update scorecard
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_calendar.events import EVENTS

_log = logging.getLogger(__name__)

DB_PATH = "/root/sovereign/data/sovereign.db"
MILESTONES = {
    "T-72h": 72.0,
    "T-24h": 24.0,
    "T-2h":  2.0,
    "T+0":   0.0,     # at event start — launches live monitor
    "T+24h": -24.0,   # negative = AFTER event
}


def _schema(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS scheduler_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT NOT NULL,
            milestone    TEXT NOT NULL,
            executed_at  TEXT NOT NULL,
            exit_code    INTEGER,
            notes        TEXT,
            UNIQUE(event_id, milestone)
        );
        """)
        conn.commit()
    finally:
        conn.close()


def already_done(event_id: str, milestone: str, db_path: str = DB_PATH) -> bool:
    _schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM scheduler_log WHERE event_id=? AND milestone=?",
            (event_id, milestone),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_done(event_id: str, milestone: str, exit_code: int, notes: str = "",
              db_path: str = DB_PATH):
    _schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO scheduler_log
               (event_id, milestone, executed_at, exit_code, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, milestone,
             datetime.now(timezone.utc).isoformat(),
             exit_code, notes),
        )
        conn.commit()
    finally:
        conn.close()


def determine_milestone(event_datetime_utc: datetime,
                        now: datetime) -> Optional[str]:
    """Return which milestone we're currently at (if any)."""
    delta_h = (event_datetime_utc - now).total_seconds() / 3600

    # T-72h to T-25h → we're in T-72h window
    if 25 <= delta_h <= 72:
        return "T-72h"
    # T-24h to T-3h → T-24h
    if 3 <= delta_h < 25:
        return "T-24h"
    # T-2h to T-0.5h → T-2h (final prep)
    if 0.5 <= delta_h < 3:
        return "T-2h"
    # T-0.5h to T+0.5h → T+0 (launch live monitor)
    if -0.5 <= delta_h < 0.5:
        return "T+0"
    # T+0.5h to T+25h → T+24h (post-event review).
    # Starts immediately after T+0 window ends so no gap — live_monitor runs
    # concurrently, post_event_review tolerates partial data.
    if -25 <= delta_h < -0.5:
        return "T+24h"
    return None


def run_milestone(event, milestone: str, bankroll: float = 5000.0) -> int:
    """Execute the prep step for a given milestone."""
    _log.info(f"[SCHED] {event.event_id} → {milestone}")

    cmd = None
    if milestone == "T-72h":
        cmd = [
            sys.executable, "/root/sovereign/prep/fomc_dry_run.py",
            "--event-id", event.event_id, "--bankroll", str(bankroll),
            "--output-json", f"/root/sovereign/logs/thesis_{event.event_id}_T72.json",
        ]
    elif milestone == "T-24h":
        # Same as T-72h but with Claude scorer (if available)
        cmd = [
            sys.executable, "/root/sovereign/prep/go_live_fomc.py",
            "--event", event.event_id, "--bankroll", str(bankroll), "--check",
            "--use-claude",
            "--output-json", f"/root/sovereign/logs/thesis_{event.event_id}_T24.json",
        ]
    elif milestone == "T-2h":
        # PAPER by default. To go live, user must set SOV_LIVE=true + manually
        # invoke go_live_fomc.py --apply --approve. Scheduler only runs paper.
        cmd = [
            sys.executable, "/root/sovereign/prep/go_live_fomc.py",
            "--event", event.event_id, "--bankroll", str(bankroll), "--paper",
            "--output-json", f"/root/sovereign/logs/orders_{event.event_id}.json",
        ]
    elif milestone == "T+0":
        # Live monitor runs for 90 min — launch as BACKGROUND process
        # (subprocess.Popen not .run), so this milestone returns immediately
        # while the monitor keeps going.
        cmd = [
            sys.executable, "/root/sovereign/prep/live_monitor.py",
            "--event", event.event_id,
            "--duration-min", "90",
            "--poll-sec", "30",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = "/root/sovereign"
        logfile = open(f"/root/sovereign/logs/live_monitor_{event.event_id}.log", "a")
        subprocess.Popen(cmd, env=env, stdout=logfile, stderr=logfile,
                          start_new_session=True)
        mark_done(event.event_id, milestone, 0,
                  "launched_background_monitor_90min")
        return 0

    elif milestone == "T+24h":
        cmd = [
            sys.executable, "/root/sovereign/prep/post_event_review.py",
            "--event", event.event_id,
            "--json", f"/root/sovereign/logs/review_{event.event_id}.json",
        ]

    if cmd is None:
        _log.error(f"no command for milestone {milestone}")
        return 99

    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/sovereign"
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    notes = f"rc={result.returncode}; stderr[:200]={result.stderr[:200] if result.stderr else ''}"
    mark_done(event.event_id, milestone, result.returncode, notes)
    return result.returncode


def tick(now: Optional[datetime] = None, bankroll: float = 5000.0):
    """One scheduler tick — look at all events, run any pending milestones."""
    now = now or datetime.now(timezone.utc)
    actions = []
    for event in EVENTS:
        milestone = determine_milestone(event.event_datetime_utc, now)
        if milestone is None:
            continue
        if already_done(event.event_id, milestone):
            continue
        rc = run_milestone(event, milestone, bankroll=bankroll)
        actions.append((event.event_id, milestone, rc))
    return actions


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--bankroll", type=float, default=5000.0)
    p.add_argument("--once", action="store_true", help="one tick then exit")
    a = p.parse_args()

    actions = tick(bankroll=a.bankroll)
    if not actions:
        print("no milestones due")
    else:
        for eid, m, rc in actions:
            print(f"{eid}  {m}  rc={rc}")


if __name__ == "__main__":
    main()
