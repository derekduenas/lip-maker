"""Weekly review of hard SERIES_BLOCKLIST entries.

Hard bans in config/settings.py never re-evaluate themselves. A series banned
in 2026-04 may have improved (rebate calibration ships, futures feed fixed,
adverse-flow trader leaves), but our config will keep blocking forever.

This tool runs weekly, computes effective_net for each banned series using:
  net_settled    = SUM(rebate + realized) from settlement_log over last 14d
  accrued_open   = SUM(_estimate_rebate) on open lip_programs in series
  effective_net  = net_settled + accrued_open

If effective_net > +$5 the series is flagged in the `review_queue` table
with status='pending_unban'. An operator reviews + manually edits config.

Why include accrued_open: settlement_log only fires at market close. Series
with long-dated open markets (KXKANYEISRAEL settles Jan 2027) would never
qualify on settled-only data, even when current snapshot scoring shows
rebate is flowing. Phase 1 finding.

USAGE:
  python blocklist_review.py             # live (writes review_queue rows)
  python blocklist_review.py --dry-run   # report only
  python blocklist_review.py --json
"""
from __future__ import annotations


# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "blocklist_review")
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
from tools.settlement_reconciler import _estimate_rebate

_log = logging.getLogger(__name__)

LOOKBACK_DAYS  = 14
UNBAN_THRESHOLD = 5.0   # effective_net > +$5 = flag for review


def _ensure_review_queue(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_queue (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            series_prefix    TEXT NOT NULL,
            status           TEXT NOT NULL,
            net_settled      REAL,
            accrued_open     REAL,
            effective_net    REAL,
            n_settled        INTEGER,
            n_open_markets   INTEGER,
            flagged_at       TEXT NOT NULL,
            breakdown_json   TEXT,
            UNIQUE(series_prefix, flagged_at)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status)"
    )
    conn.commit()


def _evaluate_series(conn: sqlite3.Connection, prefix: str) -> dict:
    """Compute net_settled + accrued_open for one banned series."""
    settled = conn.execute(f"""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(rebate_earned_usd), 0) AS rebate_total,
               COALESCE(SUM(our_realized_usd), 0) AS realized_total
        FROM settlement_log
        WHERE series_prefix LIKE ? || '%'
          AND datetime(close_time) > datetime('now', '-{LOOKBACK_DAYS} days')
    """, (prefix,)).fetchone()
    n_settled, rebate_total, realized_total = settled
    net_settled = round(rebate_total + realized_total, 2)

    open_tickers = [r[0] for r in conn.execute("""
        SELECT DISTINCT market_ticker FROM lip_programs
        WHERE market_ticker LIKE ? || '%'
          AND paid_out = 0
          AND datetime(end_date) > datetime('now')
    """, (prefix,)).fetchall()]

    accrued_open = 0.0
    for tk in open_tickers:
        try:
            accrued_open += _estimate_rebate(conn, tk)
        except Exception as e:
            _log.warning(f"_estimate_rebate({tk}) failed: {e}")
    accrued_open = round(accrued_open, 2)

    effective_net = round(net_settled + accrued_open, 2)
    return {
        "series_prefix":  prefix,
        "n_settled":      n_settled,
        "n_open_markets": len(open_tickers),
        "rebate_total":   round(rebate_total, 2),
        "realized_total": round(realized_total, 2),
        "net_settled":    net_settled,
        "accrued_open":   accrued_open,
        "effective_net":  effective_net,
        "stale":          n_settled == 0 and len(open_tickers) == 0,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Report only; do NOT insert review_queue rows")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = {
        "ts":              datetime.now(timezone.utc).isoformat(),
        "dry_run":         a.dry_run,
        "lookback_d":      LOOKBACK_DAYS,
        "unban_threshold": UNBAN_THRESHOLD,
        "evaluations":     [],
        "flagged":         [],
        "stale":           [],
    }

    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        if not a.dry_run:
            _ensure_review_queue(conn)

        flagged_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for prefix in sorted(settings.SERIES_BLOCKLIST):
            ev = _evaluate_series(conn, prefix)
            out["evaluations"].append(ev)

            if ev["stale"]:
                out["stale"].append(prefix)
                _log.warning(f"WARN: stale, can't re-evaluate: {prefix}")
                continue

            if ev["effective_net"] > UNBAN_THRESHOLD:
                out["flagged"].append(ev)
                if not a.dry_run:
                    conn.execute("""
                        INSERT OR REPLACE INTO review_queue
                        (series_prefix, status, net_settled, accrued_open,
                         effective_net, n_settled, n_open_markets,
                         flagged_at, breakdown_json)
                        VALUES (?, 'pending_unban', ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        prefix, ev["net_settled"], ev["accrued_open"],
                        ev["effective_net"], ev["n_settled"],
                        ev["n_open_markets"], flagged_at,
                        json.dumps(ev),
                    ))
        if not a.dry_run:
            conn.commit()
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n━━━ BLOCKLIST REVIEW — "
              f"{'DRY' if a.dry_run else 'LIVE'} ━━━")
        print(f"  Lookback: {LOOKBACK_DAYS}d  unban_threshold: "
              f"effective_net > +${UNBAN_THRESHOLD:.2f}")
        print(f"  Banned series scanned: {len(out['evaluations'])}\n")
        print(f"  {'series':<25} {'n_set':>5} {'n_open':>6} "
              f"{'settled':>9} {'accrued':>9} {'effective':>10}")
        for ev in sorted(out["evaluations"], key=lambda e: -e["effective_net"]):
            flag = " 🟢" if ev["effective_net"] > UNBAN_THRESHOLD else ""
            print(f"  {ev['series_prefix']:<25} {ev['n_settled']:>5} "
                  f"{ev['n_open_markets']:>6} ${ev['net_settled']:>+8.2f} "
                  f"${ev['accrued_open']:>+8.2f} ${ev['effective_net']:>+9.2f}{flag}")
        if out["flagged"]:
            print(f"\n  🟢 FLAGGED FOR REVIEW ({len(out['flagged'])}):")
            for f in out["flagged"]:
                print(f"    {f['series_prefix']:<25} effective_net=${f['effective_net']:+.2f}  "
                      f"(settled ${f['net_settled']:+.2f} + accrued ${f['accrued_open']:+.2f})")
            print(f"\n  Operator: review SQL `SELECT * FROM review_queue WHERE status='pending_unban'`")
            print(f"  then edit SERIES_BLOCKLIST in config/settings.py to unban.")
        else:
            print(f"\n  No bans qualify for unban — all banned series still net-negative.")
        if out["stale"]:
            print(f"\n  ⚠ STALE (no settlements + no open markets in {LOOKBACK_DAYS}d): {len(out['stale'])}")
            for s in out["stale"]:
                print(f"    {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
