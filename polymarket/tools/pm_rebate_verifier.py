"""PM rebate verifier — track LIP payout cadence on Polymarket US.

Per official PM US LIP docs (docs.polymarket.us/llms.txt):
  - Reward periods run midnight-to-midnight EASTERN TIME (04:00-04:00 UTC EDT)
  - Calculated within 5 BUSINESS DAYS after period end
  - Credited to account within 2 BUSINESS DAYS after calculation
  - So payout for day N lands ~N + 7 calendar days (best case)
  - Min payout $1.00 — under that gets dropped entirely
  - Cadence is per-period, NOT daily delta

This verifier:
  - Daily snapshot of balance to pm_daily_pnl_log (timeline anchor)
  - On each run: if balance > rolling_max_pre_period, attribute jump to LIP
  - Alerts on EITHER:
      (a) zero net change for 14+ days (eligibility broken)
      (b) delta > $0.50 (likely a payout) — log to pm_payouts table

USAGE:
  python tools/pm_rebate_verifier.py             # check + log
  python tools/pm_rebate_verifier.py --json      # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS
from config import settings


def get_client() -> PolymarketUS:
    return PolymarketUS(
        key_id=os.getenv("PM_API_KEY_ID"),
        secret_key=(PROJECT_ROOT / "config" / "polymarket_secret_key.b64").read_text().strip(),
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    epoch_end = yesterday  # rebate for yesterday's UTC day

    out: dict = {"ts": now.isoformat(), "epoch_end": epoch_end}

    # Live balance
    try:
        c = get_client()
        b = c.account.balances()
        cur_balance = float(b.get("balances", [{}])[0].get("currentBalance", 0))
        out["current_balance"] = cur_balance
    except Exception as e:
        out["error"] = f"balance fetch failed: {str(e)[:120]}"
        out["status"] = "ERROR"
        print(json.dumps(out, indent=2) if a.json else f"🔴 {out['error']}")
        return 2

    # Yesterday's recorded balance
    conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT balance_usd FROM pm_daily_pnl_log WHERE day = ?",
            (yesterday,),
        ).fetchone()
        prior_balance = row[0] if row else None
    finally:
        conn.close()

    out["prior_balance"] = prior_balance
    if prior_balance is None:
        out["status"] = "NO_BASELINE"
        out["msg"] = f"no recorded balance for {yesterday} — first run, will baseline tomorrow"
        print(json.dumps(out, indent=2) if a.json
              else f"📊 baseline-set @${cur_balance:.2f} (no {yesterday} record)")
        # Persist today's baseline
        conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO pm_daily_pnl_log
                   (day, realized_pnl_usd, balance_usd, snapshot_at)
                   VALUES (?, 0, ?, ?)""",
                (today, cur_balance, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            conn.commit()
        finally:
            conn.close()
        return 0

    delta = cur_balance - prior_balance
    out["delta"] = round(delta, 4)

    # Persist payout row
    conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO pm_payouts
               (epoch_end, market_slug, payout_usd, snapshot_at)
               VALUES (?, NULL, ?, ?)""",
            (epoch_end, delta, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        # Also rotate today's daily baseline
        conn.execute(
            """INSERT OR REPLACE INTO pm_daily_pnl_log
               (day, realized_pnl_usd, daily_realized_delta, balance_usd, snapshot_at)
               VALUES (?, 0, ?, ?, ?)""",
            (today, delta, cur_balance, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()
    finally:
        conn.close()

    # Days since last rebate landing (rolling 14-day check)
    conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
    try:
        last_payout_row = conn.execute(
            "SELECT MAX(snapshot_at) FROM pm_payouts WHERE payout_usd >= 1.0"
        ).fetchone()
        last_payout_iso = last_payout_row[0] if last_payout_row else None
    finally:
        conn.close()

    days_since_payout = None
    if last_payout_iso:
        try:
            last_dt = datetime.fromisoformat(last_payout_iso.replace("Z", "+00:00"))
            days_since_payout = (now - last_dt).total_seconds() / 86400
            out["days_since_last_payout"] = round(days_since_payout, 1)
        except Exception:
            pass

    # Verdict — payment cadence is 5+2 BUSINESS days, so daily delta is noise.
    # Only flag if (a) clear payout > $1 OR (b) prolonged silence > 14 days.
    if delta > 1.0:
        out["status"] = "PAYOUT"
        out["msg"] = f"+${delta:.2f} likely LIP payout"
        print(json.dumps(out, indent=2) if a.json
              else f"✅ +${delta:.2f} likely payout (was ${prior_balance:.2f}, now ${cur_balance:.2f})")
        return 0
    elif delta < -5.0:
        out["status"] = "BLEED"
        out["msg"] = f"capital LOSS ${delta:.2f} — investigate"
        print(json.dumps(out, indent=2) if a.json
              else f"🔴 BLEED ${delta:.2f} (was ${prior_balance:.2f}, now ${cur_balance:.2f})")
        return 3
    elif days_since_payout is not None and days_since_payout > 14:
        out["status"] = "NO_REBATE_14D"
        out["msg"] = (f"NO payout for {days_since_payout:.0f} days. "
                      f"Likely causes: (1) not earning ≥$1/period min, "
                      f"(2) target_size never met by aggregate, (3) eligibility revoked")
        print(json.dumps(out, indent=2) if a.json
              else f"⚠️  No PM payout for {days_since_payout:.0f}d — {out['msg']}")
        return 1
    else:
        out["status"] = "WAITING"
        out["msg"] = (f"flat ${delta:+.2f} (normal between payout periods; "
                      f"PM cadence is 7-12 cal days post-period-end)")
        print(json.dumps(out, indent=2) if a.json
              else f"📊 ${cur_balance:.2f} (Δ${delta:+.2f}, normal — PM payout cadence 7-12d)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
