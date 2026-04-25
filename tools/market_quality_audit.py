"""Pre-live market quality audit.

Analyzes lip_snapshots to identify markets that would UNDER-perform in live
mode, BEFORE we actually flip live. Three things:

  1. Auto-drop candidates: markets with valid% too low or share too thin
     — wasting WebSocket / snapshot overhead with no ROI
  2. One-sided-qualification patterns: markets where yes_qualified=1 but
     no_qualified=0 (or vice versa) for sustained periods — proxy for
     informed flow pulling one side of the book, toxic for us
  3. Share-variance flags: markets with wildly unstable share — we can't
     project income reliably if share swings ±30%

Output: decision list for `max_target_size` + explicit blacklist of toxic
markets before going live.

Run:
    cd /root/lip-maker && PYTHONPATH=. venv/bin/python tools/market_quality_audit.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)


# ─── Thresholds for auto-drop logic ──────────────────────────────────
MIN_VALID_PCT        = 0.70   # drop if fewer than 70% of snapshots qualify
MIN_AVG_SHARE        = 0.05   # drop if avg share < 5%
MIN_SNAPSHOTS        = 50     # need meaningful sample
SHARE_STDEV_LIMIT    = 0.15   # drop if share stdev > 15%
ONESIDED_THRESHOLD   = 0.40   # flag if >40% of snapshots are one-sided-qualified


def analyze(hours: int = 24, db_path: str = settings.DB_PATH) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT s.market_ticker, s.our_score, s.snapshot_valid,
                      s.yes_qualified, s.no_qualified, s.captured_at,
                      p.reward_per_day_usd, p.target_size
               FROM lip_snapshots s
               LEFT JOIN lip_programs p ON s.market_ticker = p.market_ticker
               WHERE s.captured_at >= ?""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    # Per-market bucketing
    buckets: dict[str, dict] = defaultdict(lambda: {
        "snapshots":       [],
        "shares_valid":    [],
        "yes_only":        0,    # yes_qualified=1 AND no_qualified=0
        "no_only":         0,    # no_qualified=1 AND yes_qualified=0
        "both_qualified":  0,
        "neither":         0,
        "reward":          0.0,
        "target_size":     0,
    })

    for r in rows:
        tkr, our_score, valid, yes_q, no_q, ts, reward, ts_target = r
        b = buckets[tkr]
        b["snapshots"].append((ts, valid, our_score))
        if valid == 1:
            b["shares_valid"].append(our_score / 2.0 if our_score else 0)
        if yes_q == 1 and no_q == 1:
            b["both_qualified"] += 1
        elif yes_q == 1:
            b["yes_only"] += 1
        elif no_q == 1:
            b["no_only"] += 1
        else:
            b["neither"] += 1
        b["reward"] = reward or 0
        b["target_size"] = int(ts_target or 0)

    # Classify each market
    report = {"auto_drop": [], "toxic_flow": [], "unstable": [], "keepers": []}
    total_original = 0.0
    total_after_drop = 0.0

    for tkr, b in buckets.items():
        n = len(b["snapshots"])
        if n < MIN_SNAPSHOTS:
            # Skip: not enough data
            continue

        valid_n = sum(1 for _, v, _ in b["snapshots"] if v == 1)
        valid_pct = valid_n / n

        avg_share = mean(b["shares_valid"]) if b["shares_valid"] else 0
        share_sd = stdev(b["shares_valid"]) if len(b["shares_valid"]) > 1 else 0

        # One-sided qualification pattern
        one_sided_n = b["yes_only"] + b["no_only"]
        one_sided_pct = one_sided_n / n
        one_side_dir = "yes" if b["yes_only"] > b["no_only"] else "no"
        one_side_extreme = (max(b["yes_only"], b["no_only"]) -
                            min(b["yes_only"], b["no_only"])) / max(1, n)

        # Projected revenue (current)
        proj_day = avg_share * valid_pct * b["reward"]
        total_original += proj_day

        # Classify
        entry = {
            "ticker":       tkr,
            "snapshots":    n,
            "valid_pct":    round(valid_pct, 3),
            "avg_share":    round(avg_share, 3),
            "share_stdev":  round(share_sd, 3),
            "one_sided_pct": round(one_sided_pct, 3),
            "one_side_bias": f"{one_side_dir} +{one_side_extreme*100:.0f}pp",
            "reward":       round(b["reward"], 2),
            "target_size":  b["target_size"],
            "proj_daily":   round(proj_day, 2),
        }

        # Decision logic — drop highest-priority issues first
        dropped = False

        # Reason 1: Auto-drop
        if valid_pct < MIN_VALID_PCT:
            entry["reason"] = f"low_valid%({valid_pct*100:.0f}%)"
            report["auto_drop"].append(entry)
            dropped = True
        elif avg_share < MIN_AVG_SHARE:
            entry["reason"] = f"low_share({avg_share*100:.1f}%)"
            report["auto_drop"].append(entry)
            dropped = True

        # Reason 2: Toxic flow (one-sided qualification pattern)
        if not dropped and one_side_extreme > ONESIDED_THRESHOLD:
            entry["reason"] = f"one_sided_bias ({one_side_dir} dominates)"
            report["toxic_flow"].append(entry)
            dropped = True

        # Reason 3: Unstable share — FLAG only, don't drop
        # High stdev on commodities (COCOA, CORN, WHEAT) is expected — legitimate
        # price discovery causes book-depth swings. Revenue stays in count.
        if not dropped and share_sd > SHARE_STDEV_LIMIT and avg_share < 0.15:
            # Only flag when share is small AND unstable (= competition + bad)
            entry["reason"] = f"unstable_share+thin (stdev={share_sd*100:.1f}%, share={avg_share*100:.1f}%)"
            report["unstable"].append(entry)
            dropped = True

        if not dropped:
            # High-variance but high-share markets → keeper with warning
            if share_sd > SHARE_STDEV_LIMIT:
                entry["variance_note"] = f"high_variance (stdev={share_sd*100:.1f}%)"
            report["keepers"].append(entry)
            total_after_drop += proj_day

    # Summary
    report["summary"] = {
        "markets_analyzed":     sum(len(report[k]) for k in ("auto_drop", "toxic_flow", "unstable", "keepers")),
        "auto_drop_count":      len(report["auto_drop"]),
        "toxic_flow_count":     len(report["toxic_flow"]),
        "unstable_count":       len(report["unstable"]),
        "keepers_count":        len(report["keepers"]),
        "projected_original":   round(total_original, 2),
        "projected_after_drops": round(total_after_drop, 2),
        "efficiency_gain":      round((total_after_drop/total_original - 1) * 100, 1) if total_original > 0 else 0,
    }
    return report


def print_report(report: dict, top_n: int = 10):
    s = report["summary"]
    print(f"\n══════════════════════════════════════════════════════")
    print(f"  PRE-LIVE MARKET QUALITY AUDIT")
    print(f"══════════════════════════════════════════════════════")
    print(f"  Markets analyzed:      {s['markets_analyzed']}")
    print(f"  ✅ Keepers:            {s['keepers_count']}")
    print(f"  🔴 Auto-drop:          {s['auto_drop_count']}")
    print(f"  ⚠️  Toxic flow flag:    {s['toxic_flow_count']}")
    print(f"  🟡 Unstable share:     {s['unstable_count']}")
    print()
    print(f"  Projected ORIGINAL:    ${s['projected_original']:,.2f}/day")
    print(f"  Projected AFTER DROPS: ${s['projected_after_drops']:,.2f}/day")
    print(f"  Revenue change:        {s['efficiency_gain']:+.1f}%")
    print(f"  (Variance reduction + cleaner execution)")

    def _dump(name, entries, emoji):
        if not entries:
            return
        print(f"\n  {emoji} {name} ({len(entries)} markets):")
        # Sort by original projected daily (so we see biggest losses first)
        for e in sorted(entries, key=lambda x: -x.get("proj_daily", 0))[:top_n]:
            print(f"    {e['ticker'][:38]:<40s}  "
                  f"valid%={e['valid_pct']*100:>4.0f}% "
                  f"share={e['avg_share']*100:>5.1f}% "
                  f"reward=${e['reward']:>5.0f} "
                  f"proj=${e['proj_daily']:>6.2f}  "
                  f"{e['reason']}")
        if len(entries) > top_n:
            print(f"    ... and {len(entries) - top_n} more")

    _dump("AUTO-DROP", report["auto_drop"], "🔴")
    _dump("TOXIC FLOW", report["toxic_flow"], "⚠️")
    _dump("UNSTABLE", report["unstable"], "🟡")

    print(f"\n  Top 10 KEEPERS:")
    for e in sorted(report["keepers"], key=lambda x: -x["proj_daily"])[:10]:
        print(f"    {e['ticker'][:38]:<40s}  "
              f"share={e['avg_share']*100:>5.1f}% "
              f"valid%={e['valid_pct']*100:>4.0f}% "
              f"proj=${e['proj_daily']:>6.2f}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24,
                   help="lookback window")
    p.add_argument("--top-n", type=int, default=10,
                   help="how many in each category to print")
    p.add_argument("--json", default=None, help="write full report to JSON")
    a = p.parse_args()

    report = analyze(hours=a.hours)
    print_report(report, top_n=a.top_n)

    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  JSON → {a.json}")


if __name__ == "__main__":
    main()
