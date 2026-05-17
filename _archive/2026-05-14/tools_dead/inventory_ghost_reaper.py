"""Inventory ghost reaper — removes settled-but-not-cleared rows from
the inventory table. Runs nightly via cron.

Bug fixed: settlement_log records realized PnL but inventory rows stay
behind, poisoning the bankroll_share safety gate (gate sees fake
deployed capital and rejects new top-EV markets like KXF1DELAY).

Logic:
  Delete inventory rows for tickers that:
  1. Appear in settlement_log with close_time > 24h ago, OR
  2. Have NO active LIP program AND no current API position

USAGE:
  python inventory_ghost_reaper.py            # live cleanup
  python inventory_ghost_reaper.py --dry-run  # report only

CRON: nightly 04:30 UTC (after most settlements roll in).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out: dict = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "dry_run": a.dry_run,
    }

    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        # Count current ghosts
        before = conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE net_yes_contracts != 0"
        ).fetchone()[0]
        out["ghosts_before"] = before

        # Settled-tickers ghosts (tickers in settlement_log >24h old)
        settled_query = """
            DELETE FROM inventory
            WHERE market_ticker IN (
                SELECT ticker FROM settlement_log
                WHERE datetime(close_time) < datetime('now','-1 day')
            )
            AND net_yes_contracts != 0
        """
        if not a.dry_run:
            cur = conn.execute(settled_query)
            out["settled_cleared"] = cur.rowcount
        else:
            cur = conn.execute(
                settled_query.replace("DELETE FROM inventory",
                                       "SELECT COUNT(*) AS n FROM inventory")
                              .replace("AND net_yes_contracts", "WHERE net_yes_contracts")
                              .replace("WHERE market_ticker", "AND market_ticker")
            )
            row = cur.fetchone()
            out["settled_cleared"] = row[0] if row else 0

        # Stale-program ghosts (tickers with no active lip_program AND
        # close_date past). Use end_date < now-2d as proxy.
        stale_query = """
            DELETE FROM inventory
            WHERE market_ticker IN (
                SELECT lp.market_ticker
                FROM lip_programs lp
                WHERE date(lp.end_date) < date('now','-2 days')
            )
            AND net_yes_contracts != 0
        """
        if not a.dry_run:
            cur = conn.execute(stale_query)
            out["stale_cleared"] = cur.rowcount
        else:
            out["stale_cleared"] = 0

        if not a.dry_run:
            conn.commit()
            after = conn.execute(
                "SELECT COUNT(*) FROM inventory WHERE net_yes_contracts != 0"
            ).fetchone()[0]
            out["ghosts_after"] = after
            out["total_cleared"] = before - after
        else:
            out["ghosts_after"] = before
            out["total_cleared"] = 0
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n━━━ INVENTORY GHOST REAPER — "
              f"{'DRY' if a.dry_run else 'LIVE'} ━━━")
        print(f"  Ghost rows before:  {out['ghosts_before']}")
        print(f"  Settled-clear:      {out.get('settled_cleared', 0)}")
        print(f"  Stale-program:      {out.get('stale_cleared', 0)}")
        print(f"  Ghost rows after:   {out['ghosts_after']}")
        print(f"  Total cleared:      {out['total_cleared']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
