"""Per-series auto-prune for proven losers.

Cron daily. Scans settlement_log over last 7d, identifies series net-negative
beyond a threshold, and adds them to market_blacklist for 7 days. The
existing 10s blacklist recheck in quote_manager will cancel orders within
one heartbeat.

PHILOSOPHY:
  Bleed monitor's MTM check catches OPEN-position drift (mid-cycle).
  This catches CLOSED-cycle attribution: series whose ENTIRE 7d realized
  P&L is negative beyond noise. They're paying us less rebate than they
  cost in adverse selection. Prune.

USAGE:
  python series_auto_prune.py              # check + ban
  python series_auto_prune.py --dry-run    # report only
  python series_auto_prune.py --json
"""
from __future__ import annotations


# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "series_auto_prune")
except Exception:
    pass

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

# Tunables
LOOKBACK_DAYS    = 7
NET_THRESHOLD    = -5.0   # series with 7d net < -$5 = auto-ban
MIN_SETTLEMENTS  = 3      # need at least N settlements for confidence
BAN_HOURS        = 168    # 7-day ban (re-evaluated next prune cycle)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out: dict = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "dry_run":     a.dry_run,
        "lookback_d":  LOOKBACK_DAYS,
        "threshold":   NET_THRESHOLD,
        "candidates":  [],
        "banned":      [],
        "skipped":     [],
    }

    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        # Find loser series
        rows = conn.execute(f"""
            SELECT substr(ticker, 1, instr(ticker||'-','-')-1) AS series,
                   COUNT(*) AS n,
                   ROUND(SUM(rebate_earned_usd) + SUM(our_realized_usd), 2) AS net
            FROM settlement_log
            WHERE datetime(close_time) > datetime('now','-{LOOKBACK_DAYS} days')
            GROUP BY series
            HAVING n >= {MIN_SETTLEMENTS} AND net < {NET_THRESHOLD}
            ORDER BY net
        """).fetchall()

        # Already in hard blocklist? skip
        hard_block = settings.SERIES_BLOCKLIST

        # Already in runtime blacklist (some ticker)? skip
        runtime_blocked = set()
        try:
            for r in conn.execute(
                "SELECT DISTINCT substr(ticker,1,instr(ticker||'-','-')-1) FROM market_blacklist "
                "WHERE expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            ).fetchall():
                if r[0]:
                    runtime_blocked.add(r[0])
        except Exception:
            pass

        expires_at = (datetime.now(timezone.utc) +
                      timedelta(hours=BAN_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        added_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for series, n, net in rows:
            cand = {"series": series, "n": n, "net": net}
            out["candidates"].append(cand)

            if any(series.startswith(b) for b in hard_block):
                out["skipped"].append({**cand, "reason": "in_hard_blocklist"})
                continue
            if series in runtime_blocked:
                out["skipped"].append({**cand, "reason": "already_runtime_banned"})
                continue

            # Find all open tickers in this series to ban
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT market_ticker FROM lip_programs "
                "WHERE substr(market_ticker,1,instr(market_ticker||'-','-')-1) = ? "
                "AND datetime(end_date) > datetime('now')",
                (series,),
            ).fetchall()]

            if not a.dry_run:
                reason = (f"series_auto_prune: {series} 7d net=${net:.2f} "
                          f"({n} settlements, threshold ${NET_THRESHOLD})")
                for t in tickers:
                    conn.execute(
                        "INSERT OR REPLACE INTO market_blacklist "
                        "(ticker, expires_at, reason, added_at) VALUES (?, ?, ?, ?)",
                        (t, expires_at, reason, added_at),
                    )
                conn.commit()
            out["banned"].append({**cand, "tickers_banned": len(tickers)})

    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n━━━ SERIES AUTO-PRUNE — "
              f"{'DRY' if a.dry_run else 'LIVE'} ━━━")
        print(f"  Lookback: {LOOKBACK_DAYS}d  threshold: net<${NET_THRESHOLD:.2f}  "
              f"min_settles: {MIN_SETTLEMENTS}")
        if not out["candidates"]:
            print(f"  No losing series found 🎉")
        else:
            print(f"\n  CANDIDATES ({len(out['candidates'])}):")
            for c in out["candidates"]:
                print(f"    {c['series']:<25}  n={c['n']:>3}  net=${c['net']:>+7.2f}")
            if out["banned"]:
                print(f"\n  BANNED ({len(out['banned'])}):")
                for b in out["banned"]:
                    print(f"    🚨 {b['series']:<25}  {b['tickers_banned']} tickers blacklisted {BAN_HOURS}h")
            if out["skipped"]:
                print(f"\n  SKIPPED ({len(out['skipped'])}):")
                for s in out["skipped"]:
                    print(f"    {s['series']:<25}  reason={s['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
