"""MISSED OPPORTUNITY SCANNER — daily check for high-LIP series we're not in.

WHY: The KXMIDTERMMOV miss (2026-05-06) — a $52k/day pool sat unused for 24+
hours because MIN_DISCOUNT_FACTOR=0.50 silently rejected DF=0.25 markets.
The discover() loop runs every 30min but doesn't surface WHY markets get
rejected. Without visibility, we miss whales until a human notices in UI.

THIS DAILY REPORT (cron 8:00 UTC):
  1. Top 30 LIP series by total $/day pool, NOT enrolled
  2. Reason for each (DF too low, blocklist, target too big, etc.)
  3. Total $/day we're leaving on the table
  4. Surface to logs (visible in dashboard) — humans review, adjust threshold

NO AUTO-CHANGES — just visibility. Human decides if a series is worth lowering
threshold for (some are blocked for good reason: prop-desk dominance, toxicity).
"""
from __future__ import annotations
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LIP_DB = Path("/root/lip-maker/data/lip_maker.db")
MIN_POOL_USD_PER_DAY = 50.0   # only flag series with meaningful $/day


def main():
    conn = sqlite3.connect(str(LIP_DB), timeout=10)
    # Aggregate UNENROLLED markets by series prefix
    rows = conn.execute(
        """SELECT series_ticker,
                  COUNT(*) AS n_markets,
                  ROUND(SUM(reward_per_day_usd), 2) AS total_per_day,
                  AVG(discount_factor) AS avg_df,
                  AVG(target_size) AS avg_target,
                  blocked_reason
           FROM lip_programs
           WHERE end_date > date('now') AND paid_out=0 AND enrolled=0
           GROUP BY series_ticker, blocked_reason
           HAVING total_per_day >= ?
           ORDER BY total_per_day DESC LIMIT 30""",
        (MIN_POOL_USD_PER_DAY,),
    ).fetchall()

    if not rows:
        print(f"[{datetime.now(timezone.utc).isoformat()}] ✓ No high-LIP unenrolled series. System fully covered.")
        conn.close()
        return

    total_missed = sum(r[2] for r in rows)
    print()
    print(f"[{datetime.now(timezone.utc).isoformat()}]")
    print(f"  💸 MISSED OPPORTUNITY SCANNER — TOTAL UNUSED: ${total_missed:,.0f}/day")
    print(f"  {'SERIES':<25} {'N':>5} {'$/DAY':>9} {'DF':>5} {'TARG':>5}  REASON")
    print(f"  {'-'*25} {'-'*5} {'-'*9} {'-'*5} {'-'*5}  {'-'*40}")
    by_reason = defaultdict(float)
    for s, n, total, df, tgt, why in rows:
        why_str = (why or "no_reason_logged")[:40]
        print(f"  {s[:23]:<25} {n:>5} ${total:>7.0f} {df:>5.2f} {tgt:>5.0f}  {why_str}")
        by_reason[why_str.split(":")[0]] += total

    print()
    print(f"  📊 LOSS BY ROOT CAUSE")
    for reason, dollars in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"    {reason:<35} ${dollars:>8,.0f}/day")

    # Threshold-adjustment suggestions
    print()
    print(f"  💡 SUGGESTIONS")
    df_loss = sum(t for s, n, t, df, tg, why in rows if why and "discount_too_low" in why)
    bl_loss = sum(t for s, n, t, df, tg, why in rows if why and "blocklist" in why)
    if df_loss > 100:
        print(f"    Lowering MIN_DISCOUNT_FACTOR could unlock ${df_loss:,.0f}/day (review which series)")
    if bl_loss > 100:
        print(f"    Reviewing SERIES_BLOCKLIST could unlock ${bl_loss:,.0f}/day (some may be safe to remove)")
    print()
    conn.close()


if __name__ == "__main__":
    main()
