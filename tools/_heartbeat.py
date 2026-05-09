"""Tiny helper — every watched cron calls write_heartbeat(name) at end of main()."""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone

DB = "/root/lip-maker/data/lip_maker.db"


def write_heartbeat(cron_name: str) -> None:
    try:
        conn = sqlite3.connect(DB, timeout=5.0)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cron_heartbeat (
                cron_name TEXT PRIMARY KEY,
                last_run TEXT NOT NULL,
                run_count INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            INSERT INTO cron_heartbeat (cron_name, last_run, run_count)
            VALUES (?, ?, 1)
            ON CONFLICT(cron_name) DO UPDATE SET
              last_run = excluded.last_run,
              run_count = run_count + 1
        """, (cron_name, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        # never let heartbeat failure crash the cron
        pass
