"""Polymarket account dashboard — uses official SDK.

Single command shows:
  - Account balances (USD)
  - Open positions
  - Open orders
  - Top rewards-eligible markets visible to YOUR account

USAGE:
  python tools/account_check.py
  python tools/account_check.py --markets 20  # show top N markets
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env
from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS


def get_client() -> PolymarketUS:
    key_id = os.getenv("PM_API_KEY_ID")
    secret_path = PROJECT_ROOT / "config" / "polymarket_secret_key.b64"
    if not key_id or not secret_path.exists():
        print("ERROR: PM_API_KEY_ID env or secret key file missing")
        print(f"  expected: {secret_path}")
        sys.exit(1)
    return PolymarketUS(key_id=key_id, secret_key=secret_path.read_text().strip())


def render(client: PolymarketUS, n_markets: int = 10) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  POLYMARKET US ACCOUNT — {now}")
    print(f"══════════════════════════════════════════════════════════════\n")

    # Balances
    print("📊 ACCOUNT BALANCES")
    try:
        bal = client.account.balances()
        for b in bal.get("balances", []):
            print(f"  {b.get('currency','USD'):>4s}: "
                  f"current=${b.get('currentBalance', 0):>8.2f}  "
                  f"buyingPower=${b.get('buyingPower', 0):>8.2f}  "
                  f"openOrders=${b.get('openOrders', 0):>6.2f}  "
                  f"unsettled=${b.get('unsettledFunds', 0):>6.2f}")
    except Exception as e:
        print(f"  🔴 {type(e).__name__}: {e}")

    # Positions
    print("\n📈 OPEN POSITIONS")
    try:
        pos = client.portfolio.positions()
        positions = pos.get("positions", {})
        if not positions:
            print("  (none — fresh account)")
        else:
            for slug, p in positions.items():
                print(f"  {slug}: {p}")
    except Exception as e:
        print(f"  🔴 {type(e).__name__}: {e}")

    # Open orders
    print("\n📋 OPEN ORDERS")
    try:
        o = client.orders.list({"status": "OPEN"})
        orders = o.get("orders", []) if isinstance(o, dict) else o
        if not orders:
            print("  (none)")
        else:
            for od in orders[:20]:
                slug = od.get("marketSlug", "?") if isinstance(od, dict) else "?"
                print(f"  {slug}")
    except Exception as e:
        print(f"  🔴 {type(e).__name__}: {e}")

    # Markets list
    print(f"\n🎯 MARKETS (top {n_markets})")
    try:
        m = client.markets.list({"limit": n_markets})
        markets = m.get("markets", []) if isinstance(m, dict) else m
        for mk in markets[:n_markets]:
            slug = mk.get("slug", "?") if isinstance(mk, dict) else getattr(mk, 'slug', '?')
            cat  = mk.get("category", "?") if isinstance(mk, dict) else getattr(mk, 'category', '?')
            print(f"  · [{cat}] {slug}")
    except Exception as e:
        print(f"  🔴 {type(e).__name__}: {e}")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--markets", type=int, default=10)
    a = p.parse_args()
    render(get_client(), n_markets=a.markets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
