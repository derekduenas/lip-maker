"""Macro blackout synchronizer.

Walks the macro calendar, finds currently-active blackout windows, and injects
market_blacklist rows for every enrolled market matching affected ticker prefixes.

Existing lip_discovery.top_n_to_quote filter transparently skips these.

Runs every 5 min via systemd timer. Entries auto-expire via expires_at column.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from config.macro_calendar import active_blackouts, upcoming_events

_log = logging.getLogger(__name__)


def sync_blackouts(db_path: str = settings.DB_PATH) -> dict:
    active = active_blackouts()
    conn = sqlite3.connect(db_path)
    new_blocks = 0
    already_blocked = 0
    try:
        for event in active:
            expires_iso = event["blackout_end"]
            prefixes = event["ticker_prefixes"]
            reason = f"macro_blackout:{event['name']}"

            # Find enrolled markets matching any prefix
            like_clauses = " OR ".join(["market_ticker LIKE ?" for _ in prefixes])
            params = [f"{p}%" for p in prefixes]
            markets = conn.execute(
                f"""SELECT market_ticker FROM lip_programs
                    WHERE enrolled = 1 AND paid_out = 0
                      AND ({like_clauses})""",
                params,
            ).fetchall()

            for (tkr,) in markets:
                # 2026-04-22 FIX: Use INSERT OR IGNORE + per-row commit to
                # prevent transaction-level rollback of all entries on any
                # single conflict. Previous behavior: INSERT raised
                # IntegrityError mid-loop, all 47 inserts rolled back.
                try:
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO market_blacklist
                           (ticker, expires_at, reason, added_at)
                           VALUES (?, ?, ?, datetime('now'))""",
                        (tkr, expires_iso, reason),
                    )
                    if cur.rowcount > 0:
                        new_blocks += 1
                    else:
                        already_blocked += 1
                    conn.commit()  # per-row commit — partial success preserves
                except Exception as e:
                    _log.warning(f"macro insert failed for {tkr}: {e}")
                    # don't break the loop — try the next ticker
    finally:
        conn.close()

    return {
        "active_events":   len(active),
        "new_blocks":      new_blocks,
        "already_blocked": already_blocked,
        "active_names":    [e["name"] for e in active],
    }


def show_calendar(hours_ahead: int = 48):
    print(f"\n══ MACRO CALENDAR (next {hours_ahead}h) ══")
    from config.macro_calendar import upcoming_blackouts as _up
    now = datetime.now(timezone.utc)
    act = active_blackouts()
    if act:
        print(f"\n  🔴 ACTIVE BLACKOUTS:")
        for e in act:
            print(f"    {e['name']:20s}  window: {e['blackout_start'][:16]} → {e['blackout_end'][:16]}  "
                  f"prefixes={','.join(e['ticker_prefixes'])}")
    up = _up(hours_ahead)
    if up:
        print(f"\n  🟡 UPCOMING ({len(up)}):")
        for e in up[:10]:
            mins = e["blackout_starts_in_min"]
            if mins < 60:
                when = f"{mins}min"
            elif mins < 1440:
                when = f"{mins//60}h{mins%60:02d}"
            else:
                when = f"{mins//1440}d{(mins%1440)//60}h"
            print(f"    {e['name']:20s}  in {when:>8s}  @ {e['event_utc'][:16]}  "
                  f"prefixes={','.join(e['ticker_prefixes'][:3])}{'...' if len(e['ticker_prefixes'])>3 else ''}")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--show", action="store_true", help="show upcoming calendar")
    a = p.parse_args()

    if a.show:
        show_calendar(hours_ahead=168)  # 7 days
        return

    s = sync_blackouts()
    print(f"active_events={s['active_events']}  "
          f"new_blocks={s['new_blocks']}  "
          f"already_blocked={s['already_blocked']}  "
          f"events={s['active_names']}")


if __name__ == "__main__":
    main()
