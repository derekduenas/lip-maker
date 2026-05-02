"""Balance-truth P&L — uses balance_log as ground truth instead of
settlement_log (which currently logs $0 for rebate AND realized on most
rows, so its sum is misleading).

Computes daily/weekly net cash P&L directly from balance changes. Cannot
distinguish rebate from settlement loss without external attribution, but
gives the HONEST bottom-line number that matches what hits the bank.

USAGE:
  python balance_truth.py              # human view
  python balance_truth.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def _balance_at_or_before(conn: sqlite3.Connection, iso_ts: str) -> float | None:
    r = conn.execute(
        "SELECT balance_usd FROM balance_log "
        "WHERE recorded_at <= ? ORDER BY recorded_at DESC LIMIT 1",
        (iso_ts,),
    ).fetchone()
    return r[0] if r else None


def _balance_now(conn: sqlite3.Connection) -> float | None:
    r = conn.execute(
        "SELECT balance_usd FROM balance_log ORDER BY recorded_at DESC LIMIT 1"
    ).fetchone()
    return r[0] if r else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        now_iso  = datetime.now(timezone.utc).isoformat()
        d24_iso  = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        d7_iso   = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        d30_iso  = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        cur = _balance_now(conn)
        b24 = _balance_at_or_before(conn, d24_iso)
        b7  = _balance_at_or_before(conn, d7_iso)
        b30 = _balance_at_or_before(conn, d30_iso)

        # Hourly micro-history: today's balance at each hour
        rows = conn.execute(
            "SELECT recorded_at, balance_usd FROM balance_log "
            "WHERE date(recorded_at) = date('now') ORDER BY recorded_at"
        ).fetchall()

        out = {
            "ts":               now_iso,
            "balance_now":      cur,
            "delta_24h":        round(cur - b24, 2) if cur is not None and b24 else None,
            "delta_7d":         round(cur - b7, 2) if cur is not None and b7 else None,
            "delta_30d":        round(cur - b30, 2) if cur is not None and b30 else None,
            "today_hourly":     [(r[0][:16], round(r[1], 2)) for r in rows],
        }

        # Today's biggest hourly moves (where the action happened)
        events = []
        for i in range(1, len(rows)):
            prev_t, prev_b = rows[i-1]
            cur_t, cur_b = rows[i]
            delta = cur_b - prev_b
            if abs(delta) >= 1.0:
                events.append({
                    "at":    cur_t[:16],
                    "delta": round(delta, 2),
                    "to":    round(cur_b, 2),
                })
        out["today_events"] = events
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n━━━ KALSHI BALANCE-TRUTH P&L ━━━")
        print(f"  Balance now:    ${out['balance_now']:.2f}" if out['balance_now'] else "  Balance now:    n/a")
        if out['delta_24h'] is not None:
            sign = "+" if out['delta_24h'] >= 0 else ""
            print(f"  24h delta:      {sign}${out['delta_24h']:.2f}")
        if out['delta_7d'] is not None:
            sign = "+" if out['delta_7d'] >= 0 else ""
            print(f"  7d delta:       {sign}${out['delta_7d']:.2f}   (extrap monthly: ${out['delta_7d']*30/7:+.2f})")
        if out['delta_30d'] is not None:
            sign = "+" if out['delta_30d'] >= 0 else ""
            print(f"  30d delta:      {sign}${out['delta_30d']:.2f}")
        if out['today_events']:
            print(f"\n  Today's significant balance moves (≥$1):")
            for e in out['today_events']:
                sign = "+" if e['delta'] >= 0 else ""
                kind = "💰 inflow " if e['delta'] > 0 else "🔴 outflow"
                print(f"    {e['at']}  {kind}  {sign}${e['delta']:>+8.2f}  →  ${e['to']:.2f}")
        print()
        print("  NOTE: balance delta = TRUE cash P&L. settlement_log undercounts.")
        print("        Cannot split rebate vs realized without /portfolio/activities API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
