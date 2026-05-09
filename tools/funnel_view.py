"""Render the LIP yield funnel — last hour or last day, with drop reasons.

Companion to funnel_snapshot.py. Reads funnel_log and prints a human-readable
view of the funnel. Designed to make 'where is value being lost?' obvious at
a glance.

Usage:
    python -m tools.funnel_view              # last 60 minutes
    python -m tools.funnel_view --hours 24   # last 24 hours
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


LAYER_ORDER = ["ENROLLED", "RANKED", "SAFE_5M", "SUBSCRIBED", "QUOTING",
               "TWO_SIDED", "FILLED_1H", "CLOSING_2H", "PROJECTED",
               "EST_REBATE_1H", "SETTLED_24H"]


def render(hours: int = 1, db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        # Latest snapshot for "current" view
        latest_at = conn.execute(
            "SELECT MAX(snapshot_at) FROM funnel_log"
        ).fetchone()[0]
        if not latest_at:
            print("No funnel snapshots yet. Run tools/funnel_snapshot.py first.")
            return

        latest_rows = conn.execute(
            "SELECT layer, count, detail_json FROM funnel_log WHERE snapshot_at = ?",
            (latest_at,),
        ).fetchall()
        latest = {r[0]: (r[1], json.loads(r[2] or "{}")) for r in latest_rows}

        # Median over window for stability check
        window_rows = conn.execute(
            f"""SELECT layer, count FROM funnel_log
                WHERE datetime(snapshot_at) > datetime('now','-{hours} hours')""",
        ).fetchall()
        window_counts: dict[str, list[int]] = {}
        for layer, count in window_rows:
            window_counts.setdefault(layer, []).append(count)

        print(f"\n📊 LIP YIELD FUNNEL — latest snapshot {latest_at}")
        print(f"   (window comparison: median over last {hours}h)\n")
        print(f"{'LAYER':<14} {'NOW':>6} {'MEDIAN':>7} {'DETAIL'}")
        print("─" * 110)

        prev_count = None
        for layer in LAYER_ORDER:
            if layer not in latest:
                continue
            count, detail = latest[layer]
            samples = window_counts.get(layer, [count])
            samples.sort()
            median = samples[len(samples) // 2] if samples else count
            count_str = str(count) if count >= 0 else "ERR"

            # Detail summary
            detail_str = ""
            if "pool_per_day_usd" in detail:
                detail_str = f"pool=${detail['pool_per_day_usd']:.0f}/d"
            if "drop_reasons" in detail and detail["drop_reasons"]:
                top_reason = next(iter(detail["drop_reasons"].items()))
                detail_str += f"  top_drop={top_reason[0][:35]}({top_reason[1]})"
            if "fail_reasons" in detail and detail["fail_reasons"]:
                top = next(iter(detail["fail_reasons"].items()))
                detail_str += f"  top_fail={top[0][:35]}({top[1]})"
            if "fills_by_side" in detail and detail["fills_by_side"]:
                detail_str = f"fills={detail['fills_by_side']}"
            if "net_usd" in detail:
                detail_str = (
                    f"net=${detail['net_usd']:+.2f} "
                    f"(reb=${detail.get('rebate_usd',0)} real=${detail.get('realized_usd',0)})"
                )
            if "error" in detail:
                detail_str = f"⚠ {detail['error'][:60]}"
            if "total_resting_orders" in detail:
                detail_str = (
                    f"orders={detail['total_resting_orders']} "
                    f"avg/ticker={detail['avg_orders_per_ticker']}"
                )

            # Drop arrow
            arrow = ""
            if prev_count is not None and prev_count > 0 and count >= 0:
                pct = count / prev_count * 100
                if pct < 50:
                    arrow = f"  ⚠ {pct:.0f}% retention"
                elif pct < 80:
                    arrow = f"  ⚡ {pct:.0f}% retention"
                else:
                    arrow = f"  ✓ {pct:.0f}% retention"

            print(f"{layer:<14} {count_str:>6} {median:>7}  {detail_str}{arrow}")
            if layer in {"ENROLLED", "RANKED", "SAFE_5M", "SUBSCRIBED",
                         "QUOTING", "FILLED_1H"}:
                prev_count = count if count >= 0 else prev_count

        # Pool capture rate
        if "ENROLLED" in latest and "RANKED" in latest:
            enr_pool = latest["ENROLLED"][1].get("pool_per_day_usd", 0)
            rank_pool = latest["RANKED"][1].get("pool_per_day_usd", 0)
            if enr_pool > 0:
                print(f"\n   POOL REACH: ${rank_pool:.0f}/d of ${enr_pool:.0f}/d "
                      f"({rank_pool/enr_pool*100:.0f}%) selected for quoting")
        if "SETTLED_24H" in latest:
            net = latest["SETTLED_24H"][1].get("net_usd", 0)
            reb = latest["SETTLED_24H"][1].get("rebate_usd", 0)
            real = latest["SETTLED_24H"][1].get("realized_usd", 0)
            print(f"\n   ACTUAL 24h (ours-only): ${net:+.2f} net "
                  f"(${reb} rebate + ${real} realized)")
        # Projected vs estimated-now (live snapshot rebate accrual)
        if "PROJECTED" in latest and "EST_REBATE_1H" in latest:
            proj = latest["PROJECTED"][1].get("expected_net_per_day_usd", 0)
            est = latest["EST_REBATE_1H"][1].get("total_estimated_$/d", 0)
            if proj > 0:
                ratio = est / proj if proj else 0
                drift = "✓ on-target" if 0.6 <= ratio <= 1.4 else (
                    "⚠ under-performing" if ratio < 0.6 else "🚀 over-performing")
                print(f"   PROJECTED vs LIVE-EST: allocator=${proj:.2f}/d, "
                      f"snapshot-derived=${est:.2f}/d ({ratio*100:.0f}% {drift})")
        # Projected vs settled (long-term)
        if "PROJECTED" in latest and "SETTLED_24H" in latest:
            proj = latest["PROJECTED"][1].get("expected_net_per_day_usd", 0)
            net = latest["SETTLED_24H"][1].get("net_usd", 0)
            if proj > 0 and net != 0:
                ratio = net / proj
                print(f"   PROJECTED→SETTLED: allocator=${proj:.2f}/d, "
                      f"settled=${net:+.2f}/d ({ratio*100:.0f}%)\n")
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=1)
    a = p.parse_args()
    render(hours=a.hours)
