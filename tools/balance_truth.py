"""NAV-truth P&L — uses balance_log.total_nav_usd as ground truth.

2026-05-12: migrated from balance_usd (cash only) to total_nav_usd
(cash + portfolio_value). The cash-only view was misleading because
cash drops as the engine deploys into open positions are NOT losses —
the wealth shifted into portfolio. NAV is the honest bottom line.

Computes daily/weekly NAV change directly from balance_log snapshots.
Cannot distinguish rebate from settlement loss without external
attribution, but gives the HONEST run-rate number.

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


def _nav_at_or_before(conn: sqlite3.Connection, iso_ts: str) -> tuple | None:
    """Returns (cash, portfolio_value, total_nav) at the most recent
    balance_log row <= iso_ts. Falls back to balance_usd if NAV columns
    are NULL (legacy rows pre-portfolio_value tracking)."""
    r = conn.execute(
        """SELECT balance_usd, portfolio_value_usd, total_nav_usd
           FROM balance_log WHERE recorded_at <= ?
           ORDER BY recorded_at DESC LIMIT 1""",
        (iso_ts,),
    ).fetchone()
    if not r:
        return None
    cash, pv, nav = r[0], r[1], r[2]
    if nav is None:
        # legacy row: NAV not tracked yet, use cash as best proxy
        nav = cash
    return cash, pv, nav


def _nav_now(conn: sqlite3.Connection) -> tuple | None:
    r = conn.execute(
        """SELECT balance_usd, portfolio_value_usd, total_nav_usd
           FROM balance_log ORDER BY recorded_at DESC LIMIT 1"""
    ).fetchone()
    if not r:
        return None
    cash, pv, nav = r[0], r[1], r[2]
    if nav is None:
        nav = cash
    return cash, pv, nav


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

        cur = _nav_now(conn)
        b24 = _nav_at_or_before(conn, d24_iso)
        b7  = _nav_at_or_before(conn, d7_iso)
        b30 = _nav_at_or_before(conn, d30_iso)

        # Today's hourly NAV trajectory (cash + portfolio + nav)
        rows = conn.execute(
            """SELECT recorded_at, balance_usd, portfolio_value_usd, total_nav_usd
               FROM balance_log WHERE date(recorded_at) = date('now')
               ORDER BY recorded_at"""
        ).fetchall()

        nav_now    = cur[2] if cur else None
        cash_now   = cur[0] if cur else None
        pv_now     = cur[1] if cur else None
        nav_24     = b24[2] if b24 else None
        nav_7d     = b7[2]  if b7  else None
        nav_30d    = b30[2] if b30 else None

        out = {
            "ts":               now_iso,
            "nav_now":          nav_now,
            "cash_now":         cash_now,
            "portfolio_now":    pv_now,
            "delta_24h_nav":    round(nav_now - nav_24, 2)  if nav_now is not None and nav_24 else None,
            "delta_7d_nav":     round(nav_now - nav_7d, 2)  if nav_now is not None and nav_7d else None,
            "delta_30d_nav":    round(nav_now - nav_30d, 2) if nav_now is not None and nav_30d else None,
            "today_hourly":     [
                (r[0][:16], round(r[1] or 0, 2),
                 round(r[2] or 0, 2), round(r[3] or 0, 2))
                for r in rows
            ],
        }

        # Today's biggest hourly NAV moves (where wealth actually shifted)
        events = []
        for i in range(1, len(rows)):
            prev_nav = rows[i-1][3] or rows[i-1][1] or 0
            cur_t    = rows[i][0]
            cur_nav  = rows[i][3] or rows[i][1] or 0
            delta    = cur_nav - prev_nav
            cur_b    = cur_nav    # for downstream code below
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
        print(f"\n━━━ KALSHI NAV-TRUTH P&L ━━━")
        if out['nav_now'] is not None:
            print(f"  NAV now:        ${out['nav_now']:.2f}  "
                  f"(cash ${out.get('cash_now') or 0:.2f}  +  portfolio ${out.get('portfolio_now') or 0:.2f})")
        else:
            print("  NAV now:        n/a")
        if out['delta_24h_nav'] is not None:
            sign = "+" if out['delta_24h_nav'] >= 0 else ""
            print(f"  24h delta:      {sign}${out['delta_24h_nav']:.2f}")
        if out['delta_7d_nav'] is not None:
            sign = "+" if out['delta_7d_nav'] >= 0 else ""
            print(f"  7d delta:       {sign}${out['delta_7d_nav']:.2f}   "
                  f"(extrap monthly: ${out['delta_7d_nav']*30/7:+.2f})")
        if out['delta_30d_nav'] is not None:
            sign = "+" if out['delta_30d_nav'] >= 0 else ""
            print(f"  30d delta:      {sign}${out['delta_30d_nav']:.2f}")
        if out['today_events']:
            print(f"\n  Today's significant NAV moves (≥$1):")
            for e in out['today_events']:
                sign = "+" if e['delta'] >= 0 else ""
                kind = "💰 inflow " if e['delta'] > 0 else "🔴 outflow"
                print(f"    {e['at']}  {kind}  {sign}${e['delta']:>+8.2f}  →  ${e['to']:.2f}")
        print()
        print("  NOTE: NAV delta = wealth change (cash + open-position MTM).")
        print("        Cash-only view drops as engine deploys into positions —")
        print("        that's NOT a loss. NAV is the honest bottom line.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
