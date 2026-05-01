"""Kalshi LIP sunset alarm.

Kalshi LIP program currently scheduled to end 2026-09-01. Fires escalating
alerts at T-60, T-30, T-14, T-7, T-3, T-1 days. Each fire writes to an
alerts log AND prints to stdout (which the cron pipes to its log).

Once the program is renewed, edit SUNSET_DATE constant or remove cron entry.

USAGE:
  python lip_sunset_alarm.py              # check + log if within window
  python lip_sunset_alarm.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

LIP_ROOT = Path("/root/lip-maker")
DB = str(LIP_ROOT / "data" / "lip_maker.db")

SUNSET_DATE = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
ALERT_DAYS_OUT = [60, 30, 14, 7, 3, 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    days_left = (SUNSET_DATE - now).total_seconds() / 86400

    out = {
        "now":         now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sunset":      SUNSET_DATE.strftime("%Y-%m-%d"),
        "days_left":   round(days_left, 2),
    }

    if days_left < 0:
        out["status"] = "PAST_SUNSET"
        out["msg"] = f"⛔ LIP sunset {abs(days_left):.0f} days ago. Migrate ALL Kalshi profit to PM/Gemini."
    elif any(abs(days_left - d) < 1 for d in ALERT_DAYS_OUT):
        bucket = min(ALERT_DAYS_OUT, key=lambda d: abs(d - days_left))
        out["status"] = "ALERT"
        out["bucket_days"] = bucket
        urgency = "🔴" if bucket <= 7 else "🟡" if bucket <= 30 else "🟢"
        out["msg"] = (f"{urgency} LIP sunset T-{bucket}d. "
                      f"Plan: cap Kalshi growth, prioritize PM/Gemini scaling.")
    else:
        out["status"] = "OK"
        out["msg"] = f"LIP healthy, {days_left:.0f}d to scheduled sunset"

    # Persist to alerts table for dashboard pickup
    try:
        conn = sqlite3.connect(DB, timeout=5.0)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sunset_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fired_at TEXT NOT NULL,
                days_left REAL NOT NULL,
                status TEXT NOT NULL,
                msg TEXT NOT NULL,
                UNIQUE(fired_at, status)
            )""")
        conn.execute(
            "INSERT OR IGNORE INTO sunset_alerts (fired_at, days_left, status, msg) VALUES (?,?,?,?)",
            (now.strftime("%Y-%m-%dT%H:%M:%SZ"), out["days_left"], out["status"], out["msg"]),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        out["db_error"] = str(e)[:120]

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"{out['msg']} (sunset={out['sunset']}, T-{out['days_left']:.1f}d)")
    return 0 if out["status"] != "ALERT" else 1


if __name__ == "__main__":
    sys.exit(main())
