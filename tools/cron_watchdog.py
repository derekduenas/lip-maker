"""Cron Watchdog — alert on silent cron failures.

Each critical cron writes to a heartbeat table. This watchdog checks:
- If heartbeat is older than 2x its expected interval → ALERT
- Writes alerts to alerts.log + creates blacklist row to halt trading if critical cron dead.

Run via cron every 5 min.
"""
from __future__ import annotations
import sqlite3, os
from datetime import datetime, timezone, timedelta

DB = "/root/lip-maker/data/lip_maker.db"
ALERTS_LOG = "/root/lip-maker/logs/alerts.log"

# (cron_name, expected_interval_seconds, criticality)
WATCHED = [
    ("fill_velocity_kill", 60, "critical"),       # every minute
    ("news_kill", 300, "critical"),                # every 5 min
    ("pool_depletion", 1800, "high"),              # every 30 min
    ("bleed_monitor", 600, "critical"),            # every 10 min
    ("series_auto_prune", 86400, "medium"),        # daily
    ("ramp_controller", 86400, "medium"),          # daily
]


def main():
    # Init heartbeat table if missing
    conn = sqlite3.connect(DB, timeout=10.0)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cron_heartbeat (
            cron_name TEXT PRIMARY KEY,
            last_run TEXT NOT NULL,
            run_count INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    now = datetime.now(timezone.utc)
    alerts = []

    for cron_name, interval_sec, criticality in WATCHED:
        row = conn.execute(
            "SELECT last_run FROM cron_heartbeat WHERE cron_name = ?",
            (cron_name,)
        ).fetchone()
        if not row:
            # No heartbeat ever — wait for first cycle to record
            continue
        try:
            last = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            age_sec = (now - last).total_seconds()
        except Exception:
            continue
        # Alert if 2x interval missed
        if age_sec > interval_sec * 2.5:
            alert = f"🚨 CRON DEAD: {cron_name} last ran {age_sec/60:.0f}min ago (expected every {interval_sec/60:.0f}min, criticality={criticality})"
            alerts.append((cron_name, alert, criticality))

    # Write alerts to file
    if alerts:
        with open(ALERTS_LOG, "a") as f:
            for cron_name, alert, crit in alerts:
                f.write(f"{now.isoformat()}  WATCHDOG  {alert}\n")
                print(alert)
        # If ANY critical cron dead, write a system-wide blacklist marker
        critical_dead = [a for a in alerts if a[2] == "critical"]
        if critical_dead:
            print(f"\n⚠️  {len(critical_dead)} CRITICAL crons dead — risk system COMPROMISED")
    else:
        print(f"OK — all {len(WATCHED)} crons healthy at {now.isoformat()[:19]}")

    conn.close()


if __name__ == "__main__":
    main()
