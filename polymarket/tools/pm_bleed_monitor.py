"""PM bleed monitor — out-of-band check for Polymarket auto-quoter.

Runs every 10 min via cron. Independent of in-process tier breaker so it
fires even if PM service has crashed/wedged. Two checks:

1. INTRADAY DRAWDOWN — pull current balance, compare to today-baseline.
   If drop > $X (configurable), log alert + send notification stub.
2. STALE SERVICE — if /root/polymarket-maker/logs/polymarket_maker.log
   not updated in last 5 min during expected cycle window, log alert.

Usage:
  python pm_bleed_monitor.py             # run check
  python pm_bleed_monitor.py --json
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

PM_LOG = Path("/root/polymarket-maker/logs/polymarket_maker.log")
PM_DB  = Path("/root/polymarket-maker/data/polymarket_maker.db")
DRAWDOWN_THRESHOLD_USD = 13.50  # 5% of $270 default
STALE_LOG_MINUTES = 5


def get_balance() -> float | None:
    """Subprocess to PM venv to fetch live balance."""
    pm_python = "/root/polymarket-maker/venv/bin/python"
    script = """
import os, sys
sys.path.insert(0, '/root/polymarket-maker')
from execution.pm_auth import _load_dotenv_simple
_load_dotenv_simple('/root/polymarket-maker/.env')
from polymarket_us import PolymarketUS
c = PolymarketUS(
    key_id=os.getenv('PM_API_KEY_ID'),
    secret_key=open('/root/polymarket-maker/config/polymarket_secret_key.b64').read().strip(),
)
b = c.account.balances().get('balances', [{}])[0]
print(float(b.get('currentBalance', 0)))
"""
    try:
        r = subprocess.run([pm_python, "-c", script],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    out: dict = {"ts": now.isoformat(), "checks": []}
    alerts = 0

    # Check 1: log freshness
    if not PM_LOG.exists():
        out["checks"].append({"name": "log_freshness", "status": "MISSING",
                              "msg": f"{PM_LOG} does not exist"})
        alerts += 1
    else:
        mtime = datetime.fromtimestamp(PM_LOG.stat().st_mtime, tz=timezone.utc)
        age_min = (now - mtime).total_seconds() / 60
        if age_min > STALE_LOG_MINUTES:
            out["checks"].append({"name": "log_freshness", "status": "STALE",
                                  "age_min": round(age_min, 1),
                                  "msg": f"PM log not written in {age_min:.1f}min — service stuck?"})
            alerts += 1
        else:
            out["checks"].append({"name": "log_freshness", "status": "OK",
                                  "age_min": round(age_min, 1)})

    # Check 2: intraday drawdown
    bal = get_balance()
    if bal is None:
        out["checks"].append({"name": "drawdown", "status": "NO_BALANCE",
                              "msg": "balance fetch failed"})
        alerts += 1
    else:
        out["current_balance"] = bal
        # Today's baseline from pm_daily_pnl_log
        try:
            conn = sqlite3.connect(PM_DB, timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT balance_usd FROM pm_daily_pnl_log WHERE day = ?",
                    (today,),
                ).fetchone()
                baseline = row[0] if row else None
            finally:
                conn.close()
        except Exception as e:
            baseline = None
            out["db_error"] = str(e)[:80]

        if baseline is None:
            out["checks"].append({"name": "drawdown", "status": "NO_BASELINE",
                                  "msg": "no day baseline yet"})
        else:
            loss = baseline - bal
            out["baseline"] = baseline
            out["loss"] = round(loss, 4)
            if loss > DRAWDOWN_THRESHOLD_USD:
                out["checks"].append({"name": "drawdown", "status": "ALERT",
                                      "loss": round(loss, 2),
                                      "threshold": DRAWDOWN_THRESHOLD_USD,
                                      "msg": f"🔴 PM lost ${loss:.2f} today (cap ${DRAWDOWN_THRESHOLD_USD})"})
                alerts += 1
            else:
                out["checks"].append({"name": "drawdown", "status": "OK",
                                      "loss": round(loss, 2)})

    out["alerts"] = alerts
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        if alerts:
            print(f"🔴 PM BLEED ALERT ({alerts} issue(s)):")
            for c in out["checks"]:
                if c.get("status") not in ("OK",):
                    print(f"  - {c['name']}: {c.get('msg', c.get('status'))}")
        else:
            ages = [c.get("age_min") for c in out["checks"] if c.get("name") == "log_freshness"]
            print(f"✅ PM healthy (log fresh {ages[0] if ages else '?'}min, "
                  f"loss=${out.get('loss', 0):.2f})")
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
