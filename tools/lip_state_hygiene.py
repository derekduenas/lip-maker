"""Daily LIP state hygiene — keep lip_programs row state consistent.

Why exists:
  Discovery's INSERT OR REPLACE updates only programs Kalshi's API still
  returns. Programs that disappear (Kalshi removes them, paid_out flipped,
  market closed) leave behind stale rows with enrolled=1, paid_out=0, end_date
  past. Many caller queries filter only on (enrolled=1 AND paid_out=0) and
  see those stale rows as live.

  Source fix in engine/lip_discovery.py:_decide_enrol stops new corruption
  at write time (returns enrolled=0 if end_date <= now). This cron runs
  daily as belt-and-suspenders + handles disappeared rows that discovery
  never re-touches.

What it does (idempotent):
  1. Mark expired-still-enrolled rows as paid_out=1, enrolled=0
     (matches user spec for Phase 3 cleanup)
  2. Add `stale` column if missing (default 0)
  3. Mark stale=1 where last_seen < now-7d AND stale=0
  4. Report orphans (enrolled=1 + stale=1) — should be 0 after run

USAGE:
  python lip_state_hygiene.py             # apply
  python lip_state_hygiene.py --dry-run
  python lip_state_hygiene.py --json
"""
from __future__ import annotations


# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "lip_state_hygiene")
except Exception:
    pass

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

STALE_DAYS = 7


def _ensure_stale_column(conn: sqlite3.Connection) -> bool:
    """Add `stale` INTEGER column if missing. Returns True if added."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(lip_programs)").fetchall()]
    if "stale" in cols:
        return False
    conn.execute("ALTER TABLE lip_programs ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Report only; do NOT mutate state")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = {
        "ts":              datetime.now(timezone.utc).isoformat(),
        "dry_run":         a.dry_run,
        "stale_days":      STALE_DAYS,
        "stale_col_added": False,
        "expired_marked":  0,
        "stale_marked":    0,
        "orphans":         0,
        "before":          {},
        "after":           {},
    }

    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        if not a.dry_run:
            out["stale_col_added"] = _ensure_stale_column(conn)

        # Snapshot BEFORE
        before = conn.execute("""
            SELECT
                SUM(CASE WHEN enrolled=1 AND paid_out=0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN enrolled=1 AND paid_out=0
                          AND datetime(end_date) > datetime('now') THEN 1 ELSE 0 END),
                SUM(CASE WHEN enrolled=1 AND paid_out=0
                          AND datetime(end_date) <= datetime('now') THEN 1 ELSE 0 END)
            FROM lip_programs
        """).fetchone()
        out["before"] = {
            "enrolled_unpaid_total": before[0] or 0,
            "active_quotable":       before[1] or 0,
            "expired_stale":         before[2] or 0,
        }

        # 1. Demote expired enrolled rows (per Phase 3 spec)
        expired_to_mark = conn.execute("""
            SELECT COUNT(*) FROM lip_programs
            WHERE datetime(end_date) <= datetime('now')
              AND paid_out = 0
              AND enrolled = 1
        """).fetchone()[0] or 0
        if not a.dry_run and expired_to_mark > 0:
            conn.execute("""
                UPDATE lip_programs
                SET paid_out = 1, enrolled = 0
                WHERE datetime(end_date) <= datetime('now')
                  AND paid_out = 0
                  AND enrolled = 1
            """)
            conn.commit()
        out["expired_marked"] = expired_to_mark

        # 2. Mark stale (not seen for >N days) — only if column exists post-add
        cols = [r[1] for r in conn.execute("PRAGMA table_info(lip_programs)").fetchall()]
        if "stale" in cols:
            stale_to_mark = conn.execute(f"""
                SELECT COUNT(*) FROM lip_programs
                WHERE datetime(last_seen) < datetime('now', '-{STALE_DAYS} days')
                  AND stale = 0
            """).fetchone()[0] or 0
            if not a.dry_run and stale_to_mark > 0:
                conn.execute(f"""
                    UPDATE lip_programs
                    SET stale = 1
                    WHERE datetime(last_seen) < datetime('now', '-{STALE_DAYS} days')
                      AND stale = 0
                """)
                conn.commit()
            out["stale_marked"] = stale_to_mark

            # 3. Orphans: enrolled=1 AND stale=1 (shouldn't happen post-cleanup)
            orphans = conn.execute("""
                SELECT market_ticker, last_seen, end_date
                FROM lip_programs
                WHERE enrolled = 1 AND stale = 1
                LIMIT 10
            """).fetchall()
            out["orphans"] = len(orphans)
            out["orphan_samples"] = [
                {"ticker": r[0], "last_seen": r[1], "end_date": r[2]}
                for r in orphans
            ]

        # Snapshot AFTER
        after = conn.execute("""
            SELECT
                SUM(CASE WHEN enrolled=1 AND paid_out=0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN enrolled=1 AND paid_out=0
                          AND datetime(end_date) > datetime('now') THEN 1 ELSE 0 END),
                SUM(CASE WHEN enrolled=1 AND paid_out=0
                          AND datetime(end_date) <= datetime('now') THEN 1 ELSE 0 END)
            FROM lip_programs
        """).fetchone()
        out["after"] = {
            "enrolled_unpaid_total": after[0] or 0,
            "active_quotable":       after[1] or 0,
            "expired_stale":         after[2] or 0,
        }
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n━━━ LIP STATE HYGIENE — "
              f"{'DRY' if a.dry_run else 'LIVE'} ━━━")
        if out["stale_col_added"]:
            print(f"  ✓ Added `stale` column to lip_programs")
        print(f"\n  BEFORE: enrolled+unpaid={out['before']['enrolled_unpaid_total']}  "
              f"active={out['before']['active_quotable']}  "
              f"expired_stale={out['before']['expired_stale']}")
        print(f"  Expired demoted (paid_out=1, enrolled=0): {out['expired_marked']}")
        print(f"  Stale marked (last_seen > {STALE_DAYS}d ago):   {out['stale_marked']}")
        print(f"  Orphans (enrolled=1 + stale=1):           {out['orphans']}")
        print(f"\n  AFTER:  enrolled+unpaid={out['after']['enrolled_unpaid_total']}  "
              f"active={out['after']['active_quotable']}  "
              f"expired_stale={out['after']['expired_stale']}")
        if out["orphans"] > 0:
            print(f"\n  ⚠ ORPHANS need manual review (enrolled=1 with stale=1):")
            for o in out.get("orphan_samples", []):
                print(f"    {o['ticker']:<55} last_seen={o['last_seen'][:19]}  end={o['end_date'][:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
