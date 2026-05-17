"""Daily rebate-flow alert — screams when Kalshi rebates dry up.

Catches the "dashboard says paying $X/d but UI says $0" silent failure
mode by checking actual cash flow into settlement_log + reconciling
against snapshot-projected rebates.

ALERT CONDITIONS:
  RED:    7-day Kalshi rebate sum == $0 → likely lip_programs DB stale
          OR all enrolled markets blacklisted OR quote loop broken
  YELLOW: 24h rebate < 10% of 7d-avg → unusual quiet day
  GREEN:  24h rebate within 50%-200% of 7d-avg → normal

USAGE:
  python rebate_flow_alert.py            # check + log
  python rebate_flow_alert.py --json
  python rebate_flow_alert.py --quiet    # no stdout, just alert if RED

CRON: daily at 06:00 UTC (after midnight settlements roll in)
"""
from __future__ import annotations

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json",  action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    out: dict = {"ts": datetime.now(timezone.utc).isoformat()}

    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        # 24h rebate (paid, not estimated)
        r = conn.execute("""
            SELECT ROUND(COALESCE(SUM(rebate_earned_usd),0), 2)
            FROM settlement_log
            WHERE datetime(close_time) > datetime('now','-1 day')
        """).fetchone()
        out["rebate_24h"] = r[0] or 0

        # 7d rebate
        r = conn.execute("""
            SELECT ROUND(COALESCE(SUM(rebate_earned_usd),0), 2),
                   COUNT(DISTINCT date(close_time))
            FROM settlement_log
            WHERE datetime(close_time) > datetime('now','-7 days')
        """).fetchone()
        out["rebate_7d"]   = r[0] or 0
        out["days_in_7d"]  = r[1] or 0
        out["avg_per_day"] = round(out["rebate_7d"] / max(1, out["days_in_7d"]), 2)

        # Top 5 paying markets last 7d (where the money came from)
        rows = conn.execute("""
            SELECT ticker, ROUND(SUM(rebate_earned_usd),2)
            FROM settlement_log
            WHERE datetime(close_time) > datetime('now','-7 days')
              AND rebate_earned_usd > 0
            GROUP BY ticker
            ORDER BY SUM(rebate_earned_usd) DESC LIMIT 5
        """).fetchall()
        out["top_payers_7d"] = [{"ticker": t, "rebate": r} for t, r in rows]

        # Currently quoted markets (ought to be earning)
        # Cheap proxy: last cycle's lip_snapshots count for distinct tickers
        try:
            r = conn.execute("""
                SELECT COUNT(DISTINCT market_ticker)
                FROM lip_snapshots
                WHERE datetime(captured_at) > datetime('now','-1 hour')
                  AND snapshot_valid = 1
            """).fetchone()
            out["actively_scored_markets_1h"] = r[0]
        except Exception:
            out["actively_scored_markets_1h"] = -1
    finally:
        conn.close()

    # Verdict
    avg = out["avg_per_day"]
    r24 = out["rebate_24h"]
    if out["rebate_7d"] == 0:
        out["status"] = "RED"
        out["reason"] = "ZERO Kalshi rebates in 7d — lip_programs stale, "\
                        "all markets blacklisted, or quote loop broken"
    elif avg > 0 and r24 < avg * 0.10:
        out["status"] = "YELLOW"
        out["reason"] = f"24h rebate ${r24:.2f} is <10% of 7d avg ${avg:.2f}"
    elif avg > 0 and r24 > avg * 3.0:
        out["status"] = "GREEN_SPIKE"
        out["reason"] = f"24h rebate ${r24:.2f} is 3x+ the 7d avg ${avg:.2f} — "\
                        f"verify UI matches"
    else:
        out["status"] = "GREEN"
        out["reason"] = "rebate flow normal"

    # Fire alert on RED only (quiet mode)
    if out["status"] == "RED":
        try:
            from monitor.alerts import alert as _alert
            _alert("ERROR", "rebate_flow_alert",
                   f"{out['reason']}. 24h=${r24:.2f} 7d=${out['rebate_7d']:.2f} "
                   f"actively_scored={out['actively_scored_markets_1h']}")
        except Exception as e:
            _log.warning(f"alert dispatch failed: {e}")

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    elif not a.quiet:
        print(f"\n━━━ KALSHI REBATE FLOW — {out['status']} ━━━")
        print(f"  24h rebate:               ${out['rebate_24h']:.2f}")
        print(f"  7d rebate:                ${out['rebate_7d']:.2f}")
        print(f"  7d avg/day:               ${out['avg_per_day']:.2f}")
        print(f"  Actively scored (1h):     {out['actively_scored_markets_1h']} markets")
        print(f"  Reason:                   {out['reason']}")
        if out["top_payers_7d"]:
            print(f"\n  Top 5 paying markets last 7d:")
            for tp in out["top_payers_7d"]:
                print(f"    {tp['ticker'][:50]:<50} ${tp['rebate']:.2f}")

    # Exit code: 0 green/yellow, 2 red (cron alerting)
    return 2 if out["status"] == "RED" else 0


if __name__ == "__main__":
    sys.exit(main())
