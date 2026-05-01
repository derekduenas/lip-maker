#!/usr/bin/env python3
"""NEXUS-OMNI Orchestrator — Meta-Layer Capital Router.

Computes Realized Yield Density (RYD) per venue and recommends rebalance
when one venue is materially out-yielding the other.

YIELD DENSITY FORMULA (with Bleed Factor):

    RYD = (rebate_24h - bleed_24h) / capital_deployed

Where:
    rebate_24h        = Kalshi: SUM(rebate_earned_usd) from settlement_log last 24h
                        PM:     SUM(payout_usd) from pm_payouts last 24h
    bleed_24h         = Kalshi: |SUM(realized_pnl)| where realized < 0 last 24h
                        PM:     |today_balance - day_baseline| if negative
    capital_deployed  = balance + open_exposure

Bleed Factor = realized_loss + slippage_estimate. Markets that pay rebate
but have wide spreads / informed flow taking us out → low/negative RYD.

LOGIC:
  - Every 5 min, compute RYD for both venues
  - If imbalance > REBALANCE_THRESHOLD_PCT (30%), log RECOMMEND
  - v1: alert-only. v2 (after 1 week of validation): auto-execute via
        Kalshi withdraw + PM deposit (or vice versa)

USAGE:
  python orchestrator.py             # check + log
  python orchestrator.py --json      # machine-readable
  python orchestrator.py --execute   # v2 (NOT YET IMPLEMENTED)
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
ORCH_LOG_DB = "/root/logs/orchestrator.db"  # rolling RYD history

REBALANCE_THRESHOLD_PCT = 30.0   # min RYD diff to recommend rebalance
MIN_RYD_TO_QUOTE        = 0.001  # below 0.1% daily, halt that venue


def kalshi_state() -> dict:
    """Subprocess to lip-maker venv. Returns capital + 24h rebate + 24h bleed."""
    script = """
import sys, sqlite3, json, os
sys.path.insert(0, '/root/lip-maker')
os.chdir('/root/lip-maker')
from execution.kalshi_auth import KalshiClient
c = KalshiClient()
balance = float(c.get_balance())
positions = c.get('/portfolio/positions', params={'limit':200}).get('market_positions', [])
exposure = sum(float(p.get('market_exposure_dollars', 0)) for p in positions
               if float(p.get('position_fp', 0)) != 0)
conn = sqlite3.connect('/root/lip-maker/data/lip_maker.db', timeout=5.0)
try:
    r = conn.execute(\"\"\"
        SELECT COALESCE(SUM(rebate_earned_usd),0),
               COALESCE(SUM(CASE WHEN our_realized_usd<0 THEN our_realized_usd ELSE 0 END),0),
               COALESCE(SUM(net_outcome_usd),0)
        FROM settlement_log
        WHERE datetime(close_time) > datetime('now','-1 day')
    \"\"\").fetchone()
finally:
    conn.close()
print(json.dumps({
    'balance': balance,
    'exposure': exposure,
    'capital_deployed': balance + exposure,
    'rebate_24h': r[0],
    'bleed_24h': abs(r[1]),  # absolute value of negative realized
    'net_24h': r[2],
}))
"""
    try:
        r = subprocess.run(["/root/lip-maker/venv/bin/python", "-c", script],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().split("\n")[-1])
        return {"error": (r.stderr or r.stdout)[:200]}
    except Exception as e:
        return {"error": str(e)[:120]}


def pm_state() -> dict:
    """Subprocess to PM venv. Returns capital + lifetime payouts (24h proxy)."""
    script = """
import sys, sqlite3, json, os
sys.path.insert(0, '/root/polymarket-maker')
os.chdir('/root/polymarket-maker')
from execution.pm_auth import _load_dotenv_simple
_load_dotenv_simple('/root/polymarket-maker/.env')
from polymarket_us import PolymarketUS
c = PolymarketUS(
    key_id=os.getenv('PM_API_KEY_ID'),
    secret_key=open('/root/polymarket-maker/config/polymarket_secret_key.b64').read().strip(),
)
b = c.account.balances().get('balances', [{}])[0]
balance = float(b.get('currentBalance', 0))
exposure = float(b.get('openOrders', 0)) + float(b.get('unsettledFunds', 0))
conn = sqlite3.connect('/root/polymarket-maker/data/polymarket_maker.db', timeout=5.0)
try:
    r = conn.execute(\"\"\"
        SELECT COALESCE(SUM(payout_usd),0)
        FROM pm_payouts
        WHERE datetime(snapshot_at) > datetime('now','-1 day')
          AND payout_usd > 0
    \"\"\").fetchone()
    payout_24h = r[0] or 0.0
    # Day baseline for bleed calc
    today = sys.modules['datetime'].datetime.now(sys.modules['datetime'].timezone.utc).strftime('%Y-%m-%d')
    bb = conn.execute(
        'SELECT balance_usd FROM pm_daily_pnl_log WHERE day = ?', (today,)
    ).fetchone()
    baseline = bb[0] if bb else None
finally:
    conn.close()
bleed = max(0.0, (baseline - balance)) if baseline else 0.0
print(json.dumps({
    'balance': balance,
    'exposure': exposure,
    'capital_deployed': balance + exposure,
    'rebate_24h': payout_24h,
    'bleed_24h': bleed,
    'net_24h': payout_24h - bleed,
}))
"""
    # Note: the 'datetime' lookup hack via sys.modules is to avoid breaking
    # heredoc f-string scope; just use date-aware SQLite directly instead.
    script = script.replace(
        "today = sys.modules['datetime'].datetime.now(sys.modules['datetime'].timezone.utc).strftime('%Y-%m-%d')",
        "from datetime import datetime, timezone\n    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')"
    )
    try:
        r = subprocess.run(["/root/polymarket-maker/venv/bin/python", "-c", script],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().split("\n")[-1])
        return {"error": (r.stderr or r.stdout)[:200]}
    except Exception as e:
        return {"error": str(e)[:120]}


def yield_density(s: dict) -> float | None:
    """RYD = (rebate - bleed) / capital_deployed. Daily fractional yield."""
    cap = s.get("capital_deployed", 0) or 0
    if cap < 50:  # too little capital — meaningless
        return None
    net = (s.get("rebate_24h", 0) or 0) - (s.get("bleed_24h", 0) or 0)
    return net / cap


def persist_history(now_iso: str, k: dict, p: dict, ryd_k: float | None, ryd_p: float | None) -> None:
    os.makedirs("/root/logs", exist_ok=True)
    conn = sqlite3.connect(ORCH_LOG_DB, timeout=5.0)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ryd_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_at TEXT NOT NULL,
                kalshi_capital REAL, kalshi_rebate_24h REAL, kalshi_bleed_24h REAL, kalshi_ryd REAL,
                pm_capital REAL,     pm_rebate_24h REAL,     pm_bleed_24h REAL,     pm_ryd REAL
            )
        """)
        conn.execute("""
            INSERT INTO ryd_history
            (snapshot_at, kalshi_capital, kalshi_rebate_24h, kalshi_bleed_24h, kalshi_ryd,
             pm_capital, pm_rebate_24h, pm_bleed_24h, pm_ryd)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (now_iso,
              k.get("capital_deployed"), k.get("rebate_24h"), k.get("bleed_24h"), ryd_k,
              p.get("capital_deployed"), p.get("rebate_24h"), p.get("bleed_24h"), ryd_p))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--execute", action="store_true",
                    help="(v2 — NOT IMPLEMENTED) auto-execute rebalance")
    a = ap.parse_args()

    if a.execute:
        print("🚧 --execute not implemented in v1. v1 is recommend-only.", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    k = kalshi_state()
    p = pm_state()
    ryd_k = yield_density(k) if "error" not in k else None
    ryd_p = yield_density(p) if "error" not in p else None

    out = {
        "ts":   now.isoformat(),
        "kalshi": {**k, "ryd_daily": ryd_k},
        "pm":     {**p, "ryd_daily": ryd_p},
        "recommendation": "hold",
    }

    # Imbalance check
    if ryd_k is not None and ryd_p is not None:
        # If one is positive and the other negative → migrate from negative
        if ryd_k > 0 and ryd_p <= 0:
            out["recommendation"] = "migrate_from_pm_to_kalshi"
        elif ryd_p > 0 and ryd_k <= 0:
            out["recommendation"] = "migrate_from_kalshi_to_pm"
        elif max(ryd_k, ryd_p) > 0:
            high = max(ryd_k, ryd_p)
            low  = min(ryd_k, ryd_p)
            diff_pct = (high - low) / max(0.0001, high) * 100
            if diff_pct > REBALANCE_THRESHOLD_PCT and low < high * 0.7:
                if ryd_k > ryd_p:
                    out["recommendation"] = f"shift_to_kalshi (RYD diff {diff_pct:.0f}%)"
                else:
                    out["recommendation"] = f"shift_to_pm (RYD diff {diff_pct:.0f}%)"

    persist_history(now.strftime("%Y-%m-%dT%H:%M:%SZ"), k, p, ryd_k, ryd_p)

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n🧠 NEXUS ORCHESTRATOR — {now.strftime('%H:%M UTC')}")
        if "error" in k:
            print(f"  🔴 Kalshi: {k['error']}")
        else:
            ryd_pct = (ryd_k * 100) if ryd_k is not None else 0
            print(f"  Kalshi:  cap=${k['capital_deployed']:>8.2f}  rebate24h=${k['rebate_24h']:>6.2f}  "
                  f"bleed24h=${k['bleed_24h']:>5.2f}  RYD={ryd_pct:.3f}%/d")
        if "error" in p:
            print(f"  🔴 PM:     {p['error']}")
        else:
            ryd_pct = (ryd_p * 100) if ryd_p is not None else 0
            print(f"  PM:      cap=${p['capital_deployed']:>8.2f}  rebate24h=${p['rebate_24h']:>6.2f}  "
                  f"bleed24h=${p['bleed_24h']:>5.2f}  RYD={ryd_pct:.3f}%/d")
        print(f"  → {out['recommendation']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
