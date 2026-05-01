"""Polymarket opportunity scanner — rank markets by expected $/day capture.

Reads pm_markets table populated by public_recorder, ranks by:
  expected_$/day = rewards_daily_rate × estimated_share_capture

estimated_share_capture starts at conservative 0.05 (5%) prior since we
have no observation data yet. After 1 week of running with auth + actual
quoting, this will be replaced by observed share data.

USAGE:
  python tools/opportunity_scanner.py            # top 30 by expected $/day
  python tools/opportunity_scanner.py --top 100
  python tools/opportunity_scanner.py --series   # group by event series
  python tools/opportunity_scanner.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


SHARE_PRIOR = 0.05  # 5% — conservative until we have real data


def scan(db_path: str = settings.DB_PATH) -> list[dict]:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            """SELECT market_slug, series_ticker, question,
                      reward_per_day_usd, max_spread, min_size,
                      end_date
               FROM pm_markets
               WHERE enrolled = 1 AND reward_per_day_usd > 0
               ORDER BY reward_per_day_usd DESC"""
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        slug, series, q, daily, max_spread, min_size, end = r
        expected = (daily or 0) * SHARE_PRIOR
        out.append({
            "slug":           slug,
            "series":         series or "?",
            "question":       (q or "")[:80],
            "rewards_per_day": round(daily or 0, 2),
            "expected_per_day": round(expected, 2),
            "max_spread":     max_spread,
            "min_size":       min_size,
            "end_date":       end,
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--series", action="store_true",
                   help="group by event series")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    rows = scan()

    if a.json:
        print(json.dumps(rows[:a.top], indent=2, default=str))
        return 0

    if a.series:
        by_series: dict[str, dict] = defaultdict(lambda: {"n": 0, "daily": 0.0})
        for r in rows:
            s = r["series"]
            by_series[s]["n"] += 1
            by_series[s]["daily"] += r["rewards_per_day"]
        ranked = sorted(by_series.items(), key=lambda kv: -kv[1]["daily"])
        print(f"\n══ BY EVENT SERIES (top {min(a.top, len(ranked))}) ══")
        print(f"  {'series':<55s}  {'mkts':>4s}  {'$/day pool':>12s}  {'EV @ 5%':>9s}")
        for s, d in ranked[:a.top]:
            print(f"  {s[:55]:<55s}  {d['n']:>4d}  ${d['daily']:>10.0f}  ${d['daily']*SHARE_PRIOR:>7.0f}")
        total_daily = sum(d["daily"] for d in by_series.values())
        print(f"\n  TOTAL daily pool: ${total_daily:.0f}")
        print(f"  EV at 5% capture: ${total_daily * SHARE_PRIOR:.0f}/day = ${total_daily * SHARE_PRIOR * 30:.0f}/mo")
        return 0

    print(f"\n══ POLYMARKET OPPORTUNITY SCAN — top {min(a.top, len(rows))} of {len(rows)} ══")
    print(f"  {'rank':>4s}  {'slug':<55s}  {'$/day':>8s}  {'EV @ 5%':>9s}  {'spread':>7s}  {'minSize':>8s}")
    total_pool = 0
    total_ev = 0
    for i, r in enumerate(rows[:a.top], 1):
        total_pool += r["rewards_per_day"]
        total_ev += r["expected_per_day"]
        print(f"  {i:>4d}  {r['slug'][:55]:<55s}  ${r['rewards_per_day']:>7.0f}  "
              f"${r['expected_per_day']:>7.0f}  "
              f"{r['max_spread']:>7.3f}  {r['min_size']:>8.0f}")
    print(f"\n  TOP {a.top} pool: ${total_pool:.0f}/day  |  EV @ 5% capture: ${total_ev:.0f}/day")
    print(f"  Monthly EV (top {a.top} only): ${total_ev * 30:.0f}/mo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
