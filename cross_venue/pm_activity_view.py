#!/usr/bin/env python3
"""PM activity view — equivalent of Kalshi UI's "Lifetime Rewards" page.

Shows what PM has actually paid you, broken down by:
  - Lifetime cumulative payouts
  - Last 7 daily payouts
  - Per-market payouts (when known)
  - Account activity (deposits, fills, settlements)

USAGE:
  python pm_activity_view.py            # human view
  python pm_activity_view.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PM_DB = "/root/polymarket-maker/data/polymarket_maker.db"


def pm_account_state() -> dict:
    """Subprocess to PM venv to fetch live account + activity."""
    pm_python = "/root/polymarket-maker/venv/bin/python"
    script = """
import os, sys, json
sys.path.insert(0, '/root/polymarket-maker')
from execution.pm_auth import _load_dotenv_simple
_load_dotenv_simple('/root/polymarket-maker/.env')
from polymarket_us import PolymarketUS
c = PolymarketUS(
    key_id=os.getenv('PM_API_KEY_ID'),
    secret_key=open('/root/polymarket-maker/config/polymarket_secret_key.b64').read().strip(),
)
b = c.account.balances().get('balances', [{}])[0]
out = {
    'balance': float(b.get('currentBalance', 0)),
    'buying_power': float(b.get('buyingPower', 0)),
    'unsettled': float(b.get('unsettledFunds', 0)),
    'open_orders_$': float(b.get('openOrders', 0)),
    'pending_credit': float(b.get('pendingCredit', 0)),
}
# Recent activity (deposits, settlements, etc)
try:
    a = c.portfolio.activities({'limit': 50})
    acts = a.get('activities', []) if isinstance(a, dict) else a
    out['activities'] = []
    for x in acts[:20]:
        if not isinstance(x, dict):
            continue
        t = x.get('type', '')
        bc = x.get('accountBalanceChange', {})
        amt = bc.get('amount', {}).get('value', 0) if isinstance(bc, dict) else 0
        ts = bc.get('createTime', '') if isinstance(bc, dict) else ''
        status = bc.get('status', '') if isinstance(bc, dict) else ''
        out['activities'].append({
            'type': t,
            'amount': float(amt) if amt else 0,
            'ts': ts,
            'status': status,
        })
except Exception as e:
    out['activities_err'] = str(e)[:80]
print(json.dumps(out, default=str))
"""
    try:
        r = subprocess.run([pm_python, "-c", script],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
        return {"error": (r.stderr or r.stdout)[:200]}
    except Exception as e:
        return {"error": str(e)[:120]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    pm = pm_account_state()
    out: dict = {"ts": datetime.now(timezone.utc).isoformat(), "account": pm}

    # DB-side: payouts history
    conn = sqlite3.connect(PM_DB, timeout=10.0)
    try:
        # Lifetime
        r = conn.execute(
            "SELECT COALESCE(SUM(payout_usd),0), COUNT(*) FROM pm_payouts WHERE payout_usd > 0"
        ).fetchone()
        out["lifetime_payouts"] = round(r[0] or 0, 2)
        out["lifetime_payout_count"] = r[1] or 0

        # Daily breakdown last 14 days
        rows = conn.execute("""
            SELECT epoch_end, COALESCE(SUM(payout_usd),0), COUNT(*)
            FROM pm_payouts
            WHERE payout_usd > 0
            GROUP BY epoch_end
            ORDER BY epoch_end DESC
            LIMIT 14
        """).fetchall()
        out["daily_payouts"] = [
            {"date": d, "amount": round(amt, 2), "n": n} for d, amt, n in rows
        ]

        # Settled positions today
        try:
            rows = conn.execute("""
                SELECT market_slug, net_position, realized_usd, rebate_earned_usd, net_outcome_usd
                FROM pm_settlements
                WHERE date(settled_at) >= date('now','-1 day')
                ORDER BY settled_at DESC LIMIT 15
            """).fetchall()
            out["recent_settlements"] = [
                {"slug": s, "pos": p, "real": round(r, 2), "reb": round(rb, 2), "net": round(n, 2)}
                for s, p, r, rb, n in rows
            ]
        except Exception:
            out["recent_settlements"] = []

        # Snapshot health
        r = conn.execute("""
            SELECT COUNT(*), COUNT(DISTINCT market_slug),
                   ROUND(AVG(estimated_payout_usd) * 86400, 2)
            FROM pm_snapshots
            WHERE datetime(captured_at) > datetime('now','-1 hour')
        """).fetchone()
        out["1h_snaps"] = r[0]
        out["1h_unique_mkts"] = r[1]
        out["1h_avg_proj_daily"] = r[2]
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n══════════════════════════════════════════════════════════════")
        print(f"  POLYMARKET ACTIVITY — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"══════════════════════════════════════════════════════════════\n")

        if "error" in pm:
            print(f"🔴 API: {pm['error']}")
        else:
            print(f"💰 ACCOUNT")
            print(f"  Balance:        ${pm.get('balance', 0):>9.2f}")
            print(f"  Buying power:   ${pm.get('buying_power', 0):>9.2f}")
            print(f"  Unsettled:      ${pm.get('unsettled', 0):>9.2f}")
            print(f"  Open orders:    ${pm.get('open_orders_$', 0):>9.2f}")
            print(f"  Pending credit: ${pm.get('pending_credit', 0):>9.2f}")

        print(f"\n🎟️  LIP PAYOUTS (lifetime)")
        print(f"  Cumulative:     ${out['lifetime_payouts']:>9.2f} ({out['lifetime_payout_count']} entries)")

        if out["daily_payouts"]:
            print(f"\n📅 LAST 14 DAILY PAYOUTS")
            for p in out["daily_payouts"]:
                print(f"  {p['date']}  ${p['amount']:>8.2f}  ({p['n']} entries)")
        else:
            print(f"\n📅 No daily payouts yet (next PM payout window: Apr 30 epoch lands ~May 7-12)")

        if out["recent_settlements"]:
            print(f"\n📊 RECENT SETTLED POSITIONS (last 24h)")
            for s in out["recent_settlements"][:10]:
                slug = (s['slug'] or '?')[:50]
                print(f"  {slug:<50} pos={s['pos']:>5.1f} real=${s['real']:>6.2f} reb=${s['reb']:>5.2f} net=${s['net']:>6.2f}")
        else:
            print(f"\n📊 No settlements logged in last 24h")

        # Account activity
        if pm.get("activities"):
            print(f"\n📜 RECENT ACCOUNT ACTIVITY (last 20)")
            for x in pm["activities"][:20]:
                t = x['type'].replace('ACTIVITY_TYPE_', '')[:25]
                print(f"  {x['ts'][:16]:<16} {t:<25} ${x['amount']:>8.2f} {x['status'].replace('ACCOUNT_BALANCE_CHANGE_STATUS_','')[:9]}")

        print(f"\n📡 SNAPSHOT HEALTH")
        print(f"  Last 1h:        {out['1h_snaps']} snaps in {out['1h_unique_mkts']} markets")
        print(f"  Avg projected:  ${out['1h_avg_proj_daily']:.2f}/day across markets")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
