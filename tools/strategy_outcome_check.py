#!/usr/bin/env python3
"""Strategy outcome reconciler v2 — balance-delta method.

Kalshi doesn't expose itemized incentive payouts. So we reconstruct:
  daily_balance_delta = settlement_pnl + fees + rebate_payouts
  → rebate_payouts ≈ daily_balance_delta - settlement_pnl - fees

Persists daily balance snapshot + computes inferred rebates vs estimates.
Alerts when drift > 50% over 7d.

USAGE:
  python strategy_outcome_check_v2.py        # check + log
  python strategy_outcome_check_v2.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

LIP_ROOT = Path("/root/lip-maker")
sys.path.insert(0, str(LIP_ROOT))
os.chdir(str(LIP_ROOT))

from execution.kalshi_auth import KalshiClient

LIP_DB = str(LIP_ROOT / "data" / "lip_maker.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_balance_snapshot (
    day            TEXT PRIMARY KEY,
    balance_cents  INTEGER NOT NULL,
    portfolio_cents INTEGER,
    snapshot_at    TEXT NOT NULL
);
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    out: dict = {"ts": now.isoformat()}

    # Pull current balance
    try:
        c = KalshiClient()
        b = c.get("/portfolio/balance")
        cur_balance = int(b.get("balance", 0))
        cur_portfolio = int(b.get("portfolio_value", 0))
    except Exception as e:
        out["error"] = f"balance fetch: {str(e)[:120]}"
        print(json.dumps(out) if a.json else f"🔴 {out['error']}")
        return 2

    out["balance_now"] = cur_balance / 100.0
    out["portfolio_value_now"] = cur_portfolio / 100.0

    # Persist today's snapshot
    conn = sqlite3.connect(LIP_DB, timeout=10.0)
    for stmt in SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO daily_balance_snapshot
               (day, balance_cents, portfolio_cents, snapshot_at)
               VALUES (?,?,?,?)""",
            (today, cur_balance, cur_portfolio, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()

        # Pull yesterday's snapshot
        prev = conn.execute(
            "SELECT balance_cents, portfolio_cents FROM daily_balance_snapshot WHERE day = ?",
            (yesterday,),
        ).fetchone()

        if not prev:
            out["status"] = "NO_BASELINE"
            out["msg"] = f"first snapshot today ({today}). Tomorrow's run will compare."
            print(json.dumps(out, indent=2) if a.json else f"📊 {out['msg']} balance=${cur_balance/100:.2f}")
            return 0

        prev_balance, prev_portfolio = prev
        balance_delta = (cur_balance - prev_balance) / 100.0
        portfolio_delta = (cur_portfolio - prev_portfolio) / 100.0
        # Total wealth change = settlement_pnl + rebate_payouts (fees usually small)
        total_wealth_change = balance_delta + portfolio_delta
        out["balance_delta"] = round(balance_delta, 2)
        out["portfolio_delta"] = round(portfolio_delta, 2)
        out["total_wealth_change"] = round(total_wealth_change, 2)

        # Estimated from settlement_log (yesterday's settlements + realized + rebate)
        r = conn.execute("""
            SELECT COALESCE(SUM(our_realized_usd), 0) realized,
                   COALESCE(SUM(rebate_earned_usd), 0) est_rebate
            FROM settlement_log
            WHERE date(close_time) = ?
        """, (yesterday,)).fetchone()
        est_realized = r[0] or 0
        est_rebate = r[1] or 0
        est_total = est_realized + est_rebate

        out["est_realized_yesterday"] = round(est_realized, 2)
        out["est_rebate_yesterday"]   = round(est_rebate, 2)
        out["est_total_yesterday"]    = round(est_total, 2)

        # Inferred rebate = total wealth change - settlement_realized
        inferred_rebate = total_wealth_change - est_realized
        out["inferred_rebate_actual"] = round(inferred_rebate, 2)

        # Drift between estimated and inferred actual rebate
        if abs(est_rebate) < 0.01 and abs(inferred_rebate) < 0.01:
            drift_pct = 0
        else:
            denom = max(abs(est_rebate), abs(inferred_rebate), 1.0)
            drift_pct = abs(est_rebate - inferred_rebate) / denom * 100
        out["drift_pct"] = round(drift_pct, 1)

        if drift_pct > 50:
            out["status"] = "ALERT"
            out["msg"] = (f"🚨 REBATE DRIFT {drift_pct:.0f}%: "
                          f"est ${est_rebate:.2f} vs inferred actual ${inferred_rebate:.2f}")
        else:
            out["status"] = "OK"
            out["msg"] = (f"est ${est_rebate:.2f} ≈ actual ${inferred_rebate:.2f} ({drift_pct:.0f}% drift)")
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n📊 STRATEGY OUTCOME — Kalshi (yesterday {yesterday})")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"  Balance change:         ${out.get('balance_delta', 0):>9.2f}")
        print(f"  Portfolio change:       ${out.get('portfolio_delta', 0):>9.2f}")
        print(f"  Total wealth change:    ${out.get('total_wealth_change', 0):>9.2f}")
        print(f"  Estimated realized PnL: ${out.get('est_realized_yesterday', 0):>9.2f}")
        print(f"  Estimated rebate:       ${out.get('est_rebate_yesterday', 0):>9.2f}")
        print(f"  Inferred actual rebate: ${out.get('inferred_rebate_actual', 0):>9.2f}")
        print(f"  Drift:                  {out.get('drift_pct', 0):>7.1f}%")
        print(f"  Status: {out['status']}")
        print(f"  {out['msg']}")
    return 1 if out["status"] == "ALERT" else 0


if __name__ == "__main__":
    sys.exit(main())
