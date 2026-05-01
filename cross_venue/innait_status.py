#!/usr/bin/env python3
"""INNAIT unified cross-venue status.

Single command: balance, exposure, daily/weekly NET, run-rate vs $20k/mo
target, top markets per venue, anything bleeding. Built for the morning
30-second wake-up check.

USAGE:
  ssh root@<box> "/root/innait_status.py"
  /root/innait_status.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta

LIP_DB = "/root/lip-maker/data/lip_maker.db"
PM_DB  = "/root/polymarket-maker/data/polymarket_maker.db"
TARGET_MO = 20_000.0  # $20k/mo W2-quit target


def _service_status(unit: str) -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def kalshi_state() -> dict:
    out = {"venue": "kalshi", "service": _service_status("lip-maker")}
    try:
        os.chdir("/root/lip-maker")  # KalshiClient reads relative config/ paths
        sys.path.insert(0, "/root/lip-maker")
        from execution.kalshi_auth import KalshiClient  # type: ignore
        c = KalshiClient()
        out["balance_usd"] = float(c.get_balance())
        positions = c.get("/portfolio/positions", params={"limit": 200}).get("market_positions", [])
        n_pos = sum(1 for p in positions if float(p.get("position_fp", 0)) != 0)
        exposure = sum(float(p.get("market_exposure_dollars", 0)) for p in positions
                       if float(p.get("position_fp", 0)) != 0)
        out["open_positions"] = n_pos
        out["open_exposure_usd"] = round(exposure, 2)
        orders = c.get("/portfolio/orders", params={"status": "resting", "limit": 200}).get("orders", [])
        out["resting_orders"] = len(orders)
    except Exception as e:
        out["api_error"] = str(e)[:100]

    # 7-day NET from settlement_log
    try:
        conn = sqlite3.connect(LIP_DB, timeout=5.0)
        try:
            r = conn.execute("""
                SELECT COALESCE(SUM(our_realized_usd),0),
                       COALESCE(SUM(rebate_earned_usd),0),
                       COALESCE(SUM(net_outcome_usd),0)
                FROM settlement_log
                WHERE datetime(close_time) > datetime('now','-7 days')
            """).fetchone()
            out["7d_realized"] = round(r[0], 2)
            out["7d_rebate"]   = round(r[1], 2)
            out["7d_net"]      = round(r[2], 2)
            # Active runtime blacklist from market_blacklist (if exists)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM market_blacklist WHERE expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                ).fetchone()
                out["active_runtime_blacklist"] = row[0]
            except Exception:
                pass
        finally:
            conn.close()
    except Exception as e:
        out["db_error"] = str(e)[:100]
    return out


def pm_state() -> dict:
    """Subprocess to PM venv since polymarket_us SDK lives there only.
    Avoids cross-venv import contamination."""
    out = {"venue": "polymarket", "service": _service_status("polymarket-maker")}
    pm_python = "/root/polymarket-maker/venv/bin/python"
    pm_script = """
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
orders = c.orders.list({'limit': 100}).get('orders', [])
positions = c.portfolio.positions().get('positions', {})
print(json.dumps({
    'balance_usd':    float(b.get('currentBalance', 0)),
    'buying_power':   float(b.get('buyingPower', 0)),
    'unsettled_usd':  float(b.get('unsettledFunds', 0)),
    'resting_orders': len(orders),
    'open_positions': len(positions) if isinstance(positions, dict) else 0,
}))
"""
    try:
        r = subprocess.run([pm_python, "-c", pm_script],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            out.update(json.loads(r.stdout.strip()))
        else:
            out["api_error"] = (r.stderr.strip() or r.stdout.strip() or "no output")[:120]
    except Exception as e:
        out["api_error"] = str(e)[:100]

    try:
        conn = sqlite3.connect(PM_DB, timeout=5.0)
        try:
            r = conn.execute("""
                SELECT COUNT(*),
                       COUNT(DISTINCT market_slug),
                       SUM(snapshot_valid)
                FROM pm_snapshots
                WHERE datetime(captured_at) > datetime('now','-1 hour')
            """).fetchone()
            out["1h_snapshots"]      = r[0]
            out["1h_unique_markets"] = r[1]
            out["1h_qualifying"]     = r[2] or 0
            r2 = conn.execute(
                "SELECT COALESCE(SUM(payout_usd),0) FROM pm_payouts WHERE payout_usd > 0"
            ).fetchone()
            out["lifetime_payouts"] = round(r2[0], 4)
        finally:
            conn.close()
    except Exception as e:
        out["db_error"] = str(e)[:100]
    return out


def render(k: dict, p: dict) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  INNAIT — UNIFIED CROSS-VENUE STATUS — {now}")
    print(f"══════════════════════════════════════════════════════════════\n")

    # Combined headline
    k_bal = k.get("balance_usd", 0)
    k_exp = k.get("open_exposure_usd", 0)
    p_bal = p.get("balance_usd", 0)
    total_capital = k_bal + k_exp + p_bal
    print(f"💰 TOTAL CAPITAL DEPLOYED: ${total_capital:>9.2f}")
    weekly_net_kalshi = k.get("7d_net", 0) or 0
    weekly_net_pm     = p.get("lifetime_payouts", 0) or 0  # PM payouts cumulative for now
    weekly_net_total  = weekly_net_kalshi + weekly_net_pm
    monthly_run = weekly_net_total * 30.0 / 7.0
    progress_pct = monthly_run / TARGET_MO * 100
    print(f"📈 7d NET (Kalshi+PM):     ${weekly_net_total:>9.2f}")
    print(f"📊 EXTRAP MONTHLY RUN:     ${monthly_run:>9.2f}  →  {progress_pct:5.1f}% of $20k/mo target")
    print()

    # Kalshi card
    print(f"━━━ 🟢 KALSHI ({k.get('service','?')}) ━━━")
    print(f"  Balance:           ${k.get('balance_usd', 0):>9.2f}")
    print(f"  Open exposure:     ${k.get('open_exposure_usd', 0):>9.2f}")
    print(f"  Open positions:    {k.get('open_positions', 0)}")
    print(f"  Resting orders:    {k.get('resting_orders', 0)}")
    print(f"  7d realized:       ${k.get('7d_realized', 0):>9.2f}")
    print(f"  7d rebate:         ${k.get('7d_rebate', 0):>9.2f}")
    print(f"  7d NET:            ${k.get('7d_net', 0):>9.2f}")
    if k.get("active_runtime_blacklist") is not None:
        print(f"  Runtime blacklist: {k['active_runtime_blacklist']} markets")
    if k.get("api_error"):
        print(f"  🔴 API: {k['api_error']}")

    # Polymarket card
    print(f"\n━━━ 🟡 POLYMARKET ({p.get('service','?')}) ━━━")
    print(f"  Balance:           ${p.get('balance_usd', 0):>9.2f}")
    print(f"  Buying power:      ${p.get('buying_power', 0):>9.2f}")
    print(f"  Unsettled:         ${p.get('unsettled_usd', 0):>9.2f}")
    print(f"  Open positions:    {p.get('open_positions', 0)}")
    print(f"  Resting orders:    {p.get('resting_orders', 0)}")
    print(f"  1h snapshots:      {p.get('1h_snapshots', 0)} ({p.get('1h_qualifying', 0)} qualifying, "
          f"{p.get('1h_unique_markets', 0)} markets)")
    print(f"  Lifetime payouts:  ${p.get('lifetime_payouts', 0):>9.4f}")
    if p.get("api_error"):
        print(f"  🔴 API: {p['api_error']}")

    print(f"\n━━━ NOTES ━━━")
    print(f"  PM payout cadence: 7-12 cal days post-period-end (per PM docs)")
    print(f"  Sept 1 LIP sunset: T-{(datetime(2026,9,1,tzinfo=timezone.utc) - datetime.now(timezone.utc)).days}d")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    k = kalshi_state()
    p = pm_state()
    if a.json:
        print(json.dumps({"kalshi": k, "polymarket": p}, indent=2, default=str))
    else:
        render(k, p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
