#!/usr/bin/env python3
"""Share % alert — flags markets where we're invisible.

For each market we have RESTING orders on, compute our share over the last
N hours. If share < threshold AND we have >100 snapshots there → alert.

Goal: catch the "0.0% share" pathology we discovered Apr 30, automatically.

USAGE:
  python share_alert.py             # run check + log alerts
  python share_alert.py --json
  python share_alert.py --hours 4
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

LIP_DB = "/root/lip-maker/data/lip_maker.db"
MIN_SNAPS = 100             # only flag if we have data
SHARE_ALERT_THRESHOLD = 0.001  # 0.1%


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(LIP_DB, timeout=10.0)
    out = {"ts": datetime.now(timezone.utc).isoformat(),
           "lookback_hours": a.hours,
           "alerts": [],
           "ok": []}
    try:
        rows = conn.execute(f"""
            SELECT s.market_ticker,
                   COUNT(*) AS n_snaps,
                   COALESCE(SUM(s.our_score),0) AS sum_ours,
                   COALESCE(SUM(s.total_score),0) AS sum_total
            FROM lip_snapshots s
            WHERE s.was_resting = 1
              AND s.snapshot_valid = 1
              AND datetime(s.captured_at) > datetime('now', '-{a.hours} hours')
            GROUP BY s.market_ticker
            HAVING n_snaps >= {MIN_SNAPS}
            ORDER BY sum_ours / NULLIF(sum_total,0) ASC
        """).fetchall()
    finally:
        conn.close()

    for tkr, n_snaps, sum_ours, sum_total in rows:
        if not sum_total:
            continue
        share = sum_ours / sum_total
        item = {"ticker": tkr, "n_snaps": n_snaps,
                "share_pct": round(share * 100, 4)}
        if share < SHARE_ALERT_THRESHOLD:
            out["alerts"].append(item)
        else:
            out["ok"].append(item)

    out["n_alerts"] = len(out["alerts"])
    out["n_ok"] = len(out["ok"])

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n📡 SHARE ALERT (last {a.hours}h, min {MIN_SNAPS} snaps, threshold {SHARE_ALERT_THRESHOLD*100:.2f}%)")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        if out["alerts"]:
            print(f"\n🚨 {len(out['alerts'])} INVISIBLE MARKETS (share < {SHARE_ALERT_THRESHOLD*100:.2f}%):")
            for x in out["alerts"][:20]:
                print(f"  {x['ticker'][:50]:<50} snaps={x['n_snaps']:>5} share={x['share_pct']:.4f}%")
            print(f"\n→ ACTION: drop these from selection or 5x size to claim share")
        else:
            print(f"\n✅ no invisible-market alerts ({len(out['ok'])} markets above threshold)")
        if out["ok"]:
            print(f"\n🟢 TOP MARKETS BY SHARE:")
            top = sorted(out["ok"], key=lambda x: -x["share_pct"])[:10]
            for x in top:
                print(f"  {x['ticker'][:50]:<50} snaps={x['n_snaps']:>5} share={x['share_pct']:.3f}%")
    return 1 if out["n_alerts"] > 5 else 0


if __name__ == "__main__":
    sys.exit(main())
