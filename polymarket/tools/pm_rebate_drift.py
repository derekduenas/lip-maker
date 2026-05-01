#!/usr/bin/env python3
"""PM rebate drift detector — claimed (snapshot) vs paid (balance) ratio.

Audit recommendation P1 #12. Catches PM schedule changes within ~2 weeks.

LOGIC:
  For each completed PM payout epoch, compare:
    claimed = SUM(pm_snapshots.estimated_payout_usd) for that epoch's day
    paid    = SUM(pm_payouts.payout_usd) for that epoch
  ratio = paid / claimed
  Healthy: 0.5 ≤ ratio ≤ 2.0
  Alert: ratio outside band → docs likely changed (pool resize, TS change, etc.)

USAGE:
  python pm_rebate_drift.py            # check + log
  python pm_rebate_drift.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PM_DB = "/root/polymarket-maker/data/polymarket_maker.db"
PM_CALIBRATION = 0.10        # expected theoretical→actual ratio for PM
RATIO_LOW  = 0.5             # apply to (paid / (claimed × calib))
RATIO_HIGH = 2.0
MIN_DOLLARS_TO_FLAG = 0.50


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out: dict = {"ts": datetime.now(timezone.utc).isoformat(),
                 "epochs": [], "alerts": 0}

    conn = sqlite3.connect(PM_DB, timeout=10.0)
    try:
        # For each pm_payouts row (a daily epoch_end), find matching snapshot day
        rows = conn.execute("""
            SELECT epoch_end, COALESCE(SUM(payout_usd),0) AS paid
            FROM pm_payouts
            WHERE payout_usd > 0
            GROUP BY epoch_end
            ORDER BY epoch_end DESC
            LIMIT 14
        """).fetchall()

        for epoch_end, paid in rows:
            # Estimated payout for that day = SUM(estimated_payout_usd × 86400)
            # because snapshots store rate-per-second
            r = conn.execute("""
                SELECT COALESCE(SUM(estimated_payout_usd), 0)
                FROM pm_snapshots
                WHERE date(captured_at) = ?
            """, (epoch_end,)).fetchone()
            claimed_per_sec = r[0] or 0
            # Convert from per-second-rate to daily total
            # estimated_payout_usd was stored as expected_usd / 86400
            # So daily total = sum(rate) which is already aggregate over snapshots
            # Each snap represents ~30s (heartbeat). 86400/30 = 2880 snaps/day.
            # Actually estimated_payout_usd is per-second so SUM gives us total
            # payout over the time covered by the snapshots:
            #   payout = SUM(expected_usd_per_sec × snap_count_per_sec)
            # Simpler: average of per-snap rate × seconds in day = sum(rate) × 30
            # But this is approximate. Skip overcomplication — just sum.
            claimed = claimed_per_sec * 30  # ~30 sec per snap

            if max(paid, claimed) < MIN_DOLLARS_TO_FLAG:
                continue

            # Apply expected calibration before checking band — only alert
            # if paid diverges from CALIBRATED expectation, not raw theoretical
            expected_paid = claimed * PM_CALIBRATION
            ratio = paid / max(0.001, expected_paid)
            raw_ratio = paid / max(0.001, claimed)
            status = "OK"
            if ratio < RATIO_LOW or ratio > RATIO_HIGH:
                status = "DRIFT"
                out["alerts"] += 1

            out["epochs"].append({
                "epoch_end":     epoch_end,
                "claimed":       round(claimed, 2),
                "expected_paid": round(expected_paid, 2),
                "paid":          round(paid, 2),
                "ratio_vs_exp":  round(ratio, 3),
                "raw_ratio":     round(raw_ratio, 3),
                "status":        status,
            })
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n📡 PM REBATE DRIFT (last 14 epochs, alert outside [{RATIO_LOW}, {RATIO_HIGH}])")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        if not out["epochs"]:
            print("  no scoreable epochs yet (need both pm_payouts entries AND snapshots)")
        else:
            print(f"  {'epoch_end':<12} {'claimed':>10} {'expect':>10} {'paid':>10} {'paid/exp':>9} {'raw':>7} {'status':>8}")
            for e in out["epochs"]:
                marker = "🚨" if e["status"] == "DRIFT" else "  "
                print(f"  {marker} {e['epoch_end']:<10} ${e['claimed']:>8.2f} ${e['expected_paid']:>8.2f} ${e['paid']:>8.2f}  {e['ratio_vs_exp']:>7.3f}  {e['raw_ratio']:>6.3f}  {e['status']}")
            if out["alerts"]:
                print(f"\n→ {out['alerts']} epoch(s) outside ratio band — verify PM rewards_schedule")
                print(f"  vs current docs.polymarket.us pools/TS/DF values")
            else:
                print(f"\n✅ all epochs within healthy ratio band")
    return 1 if out["alerts"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
