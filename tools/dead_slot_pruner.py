"""Dead-slot pruner — auto-cancel zero-rebate markets after holding ≥48h.

DIAGNOSIS (2026-04-27 audit, task #117):
  Capital is finite ($200 gross cap, ~20 markets at $8 avg). When dead slots
  (markets earning $0 estimated payout for 48h+) hold capacity, they crowd
  out proven winners that get rejected for "no_capital_room". Manual zombie
  audit on Apr 27 freed $72 of dead slots and immediately captured a
  $1,000-reward market — confirms the leak is real and recoverable.

DEAD-SLOT DEFINITION (post-Apr-25 phantom-fix data only):
  - market has been resting for ≥ MIN_HOURS_HELD (default 48h)
  - sum(estimated_payout_usd from snapshots where was_resting=1
    AND snapshot_valid=1) over the window < MIN_EXPECTED_PAYOUT
  - zero fills over the window (no rebate-eligible activity)

ACTION:
  Add to market_blacklist with reason="dead_slot_48h". The 10s-cached
  blacklist recheck in quote_manager (project_lip_quoteloop_gap.md fix)
  will cancel both sides within one heartbeat → frees capital for top-EV
  markets in the next reprice cycle.

GUARDS:
  - Only consider data after POST_FIX_CUTOFF (phantom snapshots inflate)
  - Skip markets that settle within SKIP_NEAR_SETTLE_MIN (capacity returns
    naturally; cancelling now leaves $0 on the table for late-window scoring)
  - Short blacklist window (24h) so a market that re-qualifies (e.g., after
    competitor leaves) can be re-enrolled the next day

Modeled after toxicity_filter.py pattern: scan + log + add_block.
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────
MIN_HOURS_HELD          = 48     # market must have been resting this long
MIN_EXPECTED_PAYOUT     = 0.50   # dollars accumulated in snapshots over window
BLACKLIST_HOURS         = 24     # short — let market re-qualify next day
POST_FIX_CUTOFF         = "2026-04-25 18:14:51"  # phantom-snapshot fix deploy
SKIP_NEAR_SETTLE_MIN    = 120    # don't cancel within 2h of settlement


SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_slot_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    hours_held      REAL NOT NULL,
    est_payout_usd  REAL NOT NULL,
    fills_count     INTEGER NOT NULL,
    snapshots_count INTEGER NOT NULL,
    action_taken    TEXT NOT NULL,   -- "blacklisted" | "skipped:<reason>"
    UNIQUE(ticker, detected_at)
);
CREATE INDEX IF NOT EXISTS idx_dead_ticker ON dead_slot_log(ticker);
CREATE INDEX IF NOT EXISTS idx_dead_time   ON dead_slot_log(detected_at);
"""


def _near_settle(ticker: str) -> bool:
    """Mirror toxicity_filter._near_settle. Skip cancellation in last 2h."""
    m = re.match(r"^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})-T", ticker)
    if not m:
        return False
    yy, mmm, dd, hh = m.groups()
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    month = months.get(mmm.upper())
    if not month:
        return False
    try:
        close_utc = datetime(2000 + int(yy), month, int(dd), int(hh) + 4,
                             tzinfo=timezone.utc)
        mins_until = (close_utc - datetime.now(timezone.utc)).total_seconds() / 60
        return 0 < mins_until < SKIP_NEAR_SETTLE_MIN
    except Exception:
        return False


def scan(db_path: str = settings.DB_PATH) -> dict:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.executescript(SCHEMA)
    conn.commit()

    now = datetime.now(timezone.utc)
    window_start = max(
        POST_FIX_CUTOFF,
        (now - timedelta(hours=MIN_HOURS_HELD)).isoformat(),
    )
    cutoff_iso = (now - timedelta(hours=MIN_HOURS_HELD)).isoformat()

    # Currently-resting markets, with earliest still-resting placed_at.
    # We require hours_held ≥ MIN_HOURS_HELD against the OLDEST resting order
    # for that market (a market we just started quoting yesterday is not "dead",
    # it's "young").
    resting = conn.execute(
        """SELECT market_ticker, MIN(placed_at) AS first_placed
           FROM quotes
           WHERE status = 'resting'
           GROUP BY market_ticker
           HAVING datetime(first_placed) <= datetime(?)""",
        (cutoff_iso,),
    ).fetchall()

    findings = []
    for ticker, first_placed in resting:
        # Estimated payout from valid resting snapshots in window
        row = conn.execute(
            """SELECT COALESCE(SUM(estimated_payout_usd), 0.0) AS payout,
                      COUNT(*) AS n_snaps
               FROM lip_snapshots
               WHERE market_ticker = ?
                 AND was_resting = 1
                 AND snapshot_valid = 1
                 AND datetime(captured_at) > datetime(?)""",
            (ticker, window_start),
        ).fetchone()
        est_payout, n_snaps = float(row[0] or 0.0), int(row[1] or 0)

        # Any fills in window? (fills mean rebate-eligible activity even if
        # snapshot data is sparse)
        fills_row = conn.execute(
            """SELECT COUNT(*) FROM fill_ledger
               WHERE ticker = ?
                 AND datetime(created_at) > datetime(?)""",
            (ticker, window_start),
        ).fetchone()
        fills_count = int(fills_row[0] or 0)

        if est_payout >= MIN_EXPECTED_PAYOUT or fills_count > 0:
            continue  # market is earning — leave alone

        # Compute hours held for the log
        try:
            placed_dt = datetime.fromisoformat(first_placed.replace("Z", "+00:00"))
            if placed_dt.tzinfo is None:
                placed_dt = placed_dt.replace(tzinfo=timezone.utc)
            hours_held = (now - placed_dt).total_seconds() / 3600
        except Exception:
            hours_held = MIN_HOURS_HELD  # fallback

        action = "blacklisted"
        skip_reason = None

        # Guards
        if _near_settle(ticker):
            skip_reason = "near_settle_window"
        else:
            existing = conn.execute(
                """SELECT 1 FROM market_blacklist
                   WHERE ticker = ?
                     AND datetime(expires_at) > datetime('now')""",
                (ticker,),
            ).fetchone()
            if existing:
                skip_reason = "already_blacklisted"

        if skip_reason:
            action = f"skipped:{skip_reason}"
        else:
            conn.execute(
                """INSERT INTO market_blacklist
                   (ticker, expires_at, reason, added_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     expires_at = excluded.expires_at,
                     reason     = excluded.reason,
                     added_at   = excluded.added_at""",
                (ticker,
                 (now + timedelta(hours=BLACKLIST_HOURS)).isoformat(),
                 f"dead_slot_48h:payout=${est_payout:.2f}_snaps={n_snaps}",
                 now.isoformat()),
            )
            _log.warning(f"dead_slot {ticker}: held {hours_held:.1f}h, "
                         f"payout=${est_payout:.2f}, fills={fills_count}, "
                         f"snaps={n_snaps} → blacklisted {BLACKLIST_HOURS}h")

        conn.execute(
            """INSERT OR IGNORE INTO dead_slot_log
               (ticker, detected_at, hours_held, est_payout_usd,
                fills_count, snapshots_count, action_taken)
               VALUES (?,?,?,?,?,?,?)""",
            (ticker, now.isoformat(), round(hours_held, 1),
             round(est_payout, 2), fills_count, n_snaps, action),
        )

        findings.append({
            "ticker":         ticker,
            "hours_held":     round(hours_held, 1),
            "est_payout":     round(est_payout, 2),
            "fills_count":    fills_count,
            "snapshots":      n_snaps,
            "action":         action,
        })

    conn.commit()

    # Lifetime stats
    pruned_24h = conn.execute(
        """SELECT COUNT(DISTINCT ticker) FROM dead_slot_log
           WHERE datetime(detected_at) > datetime('now', '-24 hour')
             AND action_taken = 'blacklisted'"""
    ).fetchone()[0]

    conn.close()
    return {
        "candidates_scanned": len(resting),
        "new_findings":       len(findings),
        "findings":           findings,
        "pruned_last_24h":    pruned_24h,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be pruned without writing blacklist")
    a = p.parse_args()

    if a.dry_run:
        # Just preview — wrap scan() write in a transaction we roll back.
        # Cheap version: open DB read-only via URI.
        conn = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True)
        try:
            now = datetime.now(timezone.utc)
            cutoff_iso = (now - timedelta(hours=MIN_HOURS_HELD)).isoformat()
            window_start = max(POST_FIX_CUTOFF, cutoff_iso)
            resting = conn.execute(
                """SELECT market_ticker, MIN(placed_at)
                   FROM quotes WHERE status='resting'
                   GROUP BY market_ticker
                   HAVING datetime(MIN(placed_at)) <= datetime(?)""",
                (cutoff_iso,),
            ).fetchall()
            print(f"\n══ DEAD-SLOT DRY RUN ══")
            print(f"  candidates (resting >{MIN_HOURS_HELD}h): {len(resting)}")
            for ticker, first_placed in resting:
                row = conn.execute(
                    """SELECT COALESCE(SUM(estimated_payout_usd),0), COUNT(*)
                       FROM lip_snapshots
                       WHERE market_ticker=? AND was_resting=1
                         AND snapshot_valid=1
                         AND datetime(captured_at) > datetime(?)""",
                    (ticker, window_start),
                ).fetchone()
                if row[0] < MIN_EXPECTED_PAYOUT:
                    print(f"    DEAD  {ticker:<40s}  payout=${row[0]:.2f}  snaps={row[1]}")
        finally:
            conn.close()
        return 0

    r = scan()
    print(f"\n══ DEAD-SLOT PRUNER ══")
    print(f"  candidates scanned:   {r['candidates_scanned']}")
    print(f"  new findings:         {r['new_findings']}")
    print(f"  pruned last 24h:      {r['pruned_last_24h']}")
    if r["findings"]:
        print(f"\n  💀 Markets pruned this cycle:")
        for f in r["findings"]:
            print(f"    {f['ticker'][:40]:<42s}  "
                  f"held={f['hours_held']:>5.1f}h  "
                  f"payout=${f['est_payout']:>5.2f}  "
                  f"snaps={f['snapshots']:>4d}  "
                  f"→ {f['action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
