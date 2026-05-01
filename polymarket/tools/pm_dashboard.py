"""Polymarket dashboard — parity with Kalshi tools/dashboard.py.

Single command shows everything needed to monitor the Polymarket
auto-quoter at a glance. Pulls live state from API + recent activity
from local pm_snapshots.

USAGE:
  python tools/pm_dashboard.py
  python tools/pm_dashboard.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS


def get_client() -> PolymarketUS:
    return PolymarketUS(
        key_id=os.getenv("PM_API_KEY_ID"),
        secret_key=(PROJECT_ROOT / "config" / "polymarket_secret_key.b64").read_text().strip(),
    )


def fetch_account(client: PolymarketUS) -> dict:
    out = {}
    try:
        b = client.account.balances()
        out["balance"] = b.get("balances", [{}])[0]
    except Exception as e:
        out["balance_error"] = str(e)[:120]
    try:
        p = client.portfolio.positions()
        positions = p.get("positions", {})
        out["positions"] = positions
    except Exception as e:
        out["positions_error"] = str(e)[:120]
    try:
        o = client.orders.list({"limit": 200})
        orders = o.get("orders", []) if isinstance(o, dict) else o
        out["orders"] = orders
    except Exception as e:
        out["orders_error"] = str(e)[:120]
    return out


def fetch_local_snapshot_state(db_path: str) -> dict:
    out = {}
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        # Last hour activity
        out["last_hour_snapshots"] = conn.execute(
            """SELECT COUNT(*) FROM pm_snapshots
               WHERE datetime(captured_at) > datetime('now','-1 hour')"""
        ).fetchone()[0]
        out["last_hour_qualifying"] = conn.execute(
            """SELECT COUNT(*) FROM pm_snapshots
               WHERE datetime(captured_at) > datetime('now','-1 hour')
                 AND snapshot_valid=1"""
        ).fetchone()[0]
        # Top markets by AVG daily-rate EV in last hour.
        # estimated_payout_usd is stored as (expected_usd_daily / 86400) per snap.
        # AVG × 86400 = mean daily rate. SUM × 86400 (the old bug) =
        # daily-rate × snapshot-count which is meaningless and inflates 50-100x.
        rows = conn.execute(
            """SELECT market_slug,
                      COUNT(*) AS n_snaps,
                      ROUND(AVG(estimated_payout_usd) * 86400, 2) AS extrap_daily,
                      ROUND(AVG(midpoint), 3) AS avg_mid,
                      MAX(snapshot_valid) AS qualified
               FROM pm_snapshots
               WHERE datetime(captured_at) > datetime('now','-1 hour')
               GROUP BY market_slug
               HAVING extrap_daily > 0
               ORDER BY extrap_daily DESC
               LIMIT 10"""
        ).fetchall()
        out["top_markets"] = rows
        # Markets discovered (recorder)
        out["recorder_markets"] = conn.execute(
            """SELECT COUNT(*) FROM pm_markets
               WHERE enrolled = 1 AND reward_per_day_usd > 0"""
        ).fetchone()[0]
        conn.close()
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def render(account: dict, local: dict) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  POLYMARKET AUTO-QUOTER DASHBOARD — {now}")
    print(f"══════════════════════════════════════════════════════════════\n")

    # Account
    print("📊 ACCOUNT")
    bal = account.get("balance", {})
    if bal:
        print(f"  Balance:        ${bal.get('currentBalance', 0):.2f}")
        print(f"  Buying power:   ${bal.get('buyingPower', 0):.2f}")
        print(f"  Open orders $:  ${bal.get('openOrders', 0):.2f}")
        print(f"  Unsettled $:    ${bal.get('unsettledFunds', 0):.2f}")
    elif account.get("balance_error"):
        print(f"  🔴 {account['balance_error']}")

    # Positions
    positions = account.get("positions", {})
    n_pos = len(positions) if isinstance(positions, dict) else 0
    print(f"\n📈 OPEN POSITIONS: {n_pos}")
    if n_pos > 0:
        for slug, p in list(positions.items())[:10]:
            print(f"  {slug}")

    # Open orders
    orders = account.get("orders", [])
    print(f"\n📋 RESTING ORDERS: {len(orders)}")
    from collections import defaultdict
    by_slug = defaultdict(int)
    for o in orders:
        by_slug[o.get("marketSlug", "?")] += 1
    for slug, n in sorted(by_slug.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {slug:<55s} {n} orders")

    # Snapshot health
    print(f"\n📊 SNAPSHOT CAPTURE (last hour)")
    print(f"  Total snapshots:     {local.get('last_hour_snapshots', 0)}")
    print(f"  Qualifying snapshots: {local.get('last_hour_qualifying', 0)}")
    print(f"  Recorder markets:    {local.get('recorder_markets', 0)}")

    # Top earning markets
    top = local.get("top_markets", []) or []
    print(f"\n💰 TOP MARKETS BY EXPECTED $/DAY (last hour)")
    if top:
        print(f"  {'slug':<48s}  {'n_snaps':>7s}  {'$/d':>8s}  {'mid':>5s}  {'qual':>4s}")
        total_ev = 0
        for slug, n, ev, mid, q in top:
            total_ev += ev or 0
            print(f"  {slug[:48]:<48s}  {n:>7d}  ${ev or 0:>6.2f}  {mid or 0:>5.3f}  {'✓' if q else '✗':>4s}")
        print(f"  {'─'*48}")
        print(f"  TOTAL extrap'd daily EV (top 10): ${total_ev:.2f}")
    else:
        print(f"  (no qualifying snapshot data — quoter may be paper or down)")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    os.chdir(str(PROJECT_ROOT))
    client = get_client()
    account = fetch_account(client)
    local = fetch_local_snapshot_state(str(PROJECT_ROOT / "data" / "polymarket_maker.db"))

    if a.json:
        out = {
            "account": {
                "balance":   account.get("balance"),
                "n_orders":  len(account.get("orders", [])),
                "n_positions": len(account.get("positions", {})) if isinstance(account.get("positions"), dict) else 0,
            },
            "local": {
                "last_hour_snapshots": local.get("last_hour_snapshots", 0),
                "last_hour_qualifying": local.get("last_hour_qualifying", 0),
                "top_markets": local.get("top_markets", []),
            },
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        render(account, local)
    return 0


if __name__ == "__main__":
    sys.exit(main())
