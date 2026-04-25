"""Analyze LIP paper performance per market to identify winners/losers.

Reads from `lip_snapshots` (just started being populated) + `lip_programs`
to rank the 100 markets we're quoting by actual paper scoring. Output:
  - Markets where we're getting meaningful share (≥10%) on valid snapshots
  - Markets where we're present but not qualifying (both sides don't meet target)
  - Markets where we're consistently dominated (<5% share)

Use: drop bottom performers, concentrate on top. Dynamic filter beats
static top-N ranking because reward-per-day doesn't predict our scoring
success — competition dominates.

Projected payout for each = (our_avg_share_normalized × reward_per_day).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def analyze(hours_back: int = 2, db_path: str = settings.DB_PATH):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        # Rollup per market
        rows = conn.execute(
            """SELECT s.market_ticker,
                      COUNT(*)                    AS snaps,
                      SUM(s.snapshot_valid)       AS valid_snaps,
                      AVG(s.our_score)            AS avg_score,
                      AVG(CASE WHEN s.snapshot_valid=1 THEN s.our_score END) AS avg_valid_score,
                      SUM(s.estimated_payout_usd) AS sum_payout_slice,
                      p.reward_per_day_usd,
                      p.target_size
               FROM lip_snapshots s
               LEFT JOIN lip_programs p ON s.market_ticker = p.market_ticker
               WHERE s.captured_at >= ?
               GROUP BY s.market_ticker
               ORDER BY avg_valid_score DESC""",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No lip_snapshots data yet (or empty window). Run paper runner first.")
        return

    print(f"\n══════════════════════════════════════════════════════")
    print(f"  LIP market performance — last {hours_back}h")
    print(f"══════════════════════════════════════════════════════\n")

    print(f"{'market':<40s} {'snaps':>5s} {'valid%':>6s} {'our_sh':>6s} {'reward':>6s} {'proj$/d':>8s} {'ts':>5s}")
    total_proj_day = 0.0
    winners = []
    losers  = []
    empty   = []

    for r in rows:
        tkr = r[0]
        snaps = r[1]
        valid = r[2] or 0
        avg_score = r[3] or 0  # 0-2.0 range (both sides)
        avg_valid = r[4] or 0
        reward = r[6] or 0
        target = r[7] or 0

        valid_pct = (valid / snaps) if snaps else 0
        # Share is our_score / 2.0 (max 100% both sides)
        our_share = avg_valid / 2.0 if avg_valid else 0
        # Projected daily payout = share × valid% × reward
        proj_day = our_share * valid_pct * reward
        total_proj_day += proj_day

        print(f"  {tkr[:38]:<38s} {snaps:>5d} {valid_pct*100:>5.0f}% {our_share*100:>5.1f}% "
              f"${reward:>5.0f} ${proj_day:>6.2f}  {int(target):>5d}")

        if valid_pct < 0.3:
            empty.append((tkr, valid_pct, snaps, reward))
        elif our_share >= 0.10:
            winners.append((tkr, our_share, proj_day, reward))
        elif our_share < 0.03:
            losers.append((tkr, our_share, proj_day, reward))

    print(f"\n  PROJECTED PAPER TOTAL: ${total_proj_day:.2f}/day\n")

    print(f"── Winners (share ≥ 10%, valid%≥30%) — {len(winners)} markets ──")
    for tkr, share, proj, rew in sorted(winners, key=lambda x: -x[2])[:15]:
        print(f"  {tkr[:40]:<40s}  share={share*100:5.1f}%  proj=${proj:6.2f}  reward=${rew:5.0f}")

    print(f"\n── Dominated (share < 3% or never qualify) — DROP CANDIDATES — {len(losers)+len(empty)} ──")
    for tkr, share, proj, rew in sorted(losers, key=lambda x: x[1])[:8]:
        print(f"  LOW-SHARE  {tkr[:35]:<35s}  share={share*100:5.1f}%  reward=${rew:5.0f}")
    for tkr, vp, sn, rew in sorted(empty, key=lambda x: x[1])[:8]:
        print(f"  NO-QUALIFY {tkr[:35]:<35s}  valid%={vp*100:>4.0f}% (snaps={sn}) reward=${rew:5.0f}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=2)
    a = p.parse_args()
    analyze(hours_back=a.hours)
