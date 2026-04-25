"""LIP pool analyzer — what's the opportunity distribution across all 373 markets?

Helps answer:
  - Which markets are in our top-50 quote list vs. which are missed?
  - What's the marginal reward of expanding to top-75 / top-100 / top-N?
  - Per-category breakdown (which market types have the highest concentration)?

Output: ranked markets with cumulative reward pool captured.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def rank_programs():
    conn = sqlite3.connect(settings.DB_PATH)
    try:
        rows = conn.execute(
            """SELECT market_ticker, reward_per_day_usd, target_size, discount_factor,
                      series_ticker
               FROM lip_programs
               WHERE enrolled = 1
               ORDER BY reward_per_day_usd DESC"""
        ).fetchall()
    finally:
        conn.close()
    return rows


def series_prefix(ticker: str) -> str:
    """KXTRUMPPHOTO-26APR25 → KXTRUMPPHOTO"""
    return ticker.split("-", 1)[0] if "-" in ticker else ticker


def main():
    rows = rank_programs()
    if not rows:
        print("No enrolled programs. Run discover() first.")
        return

    total_pool = sum(r[1] for r in rows)
    print(f"\n══════════════════════════════════════════════")
    print(f"  LIP Pool: {len(rows)} markets, ${total_pool:,.0f}/day total")
    print(f"══════════════════════════════════════════════\n")

    print(f"{'rank':>4s}  {'reward':>8s}  {'cumul%':>7s}  {'ts':>6s}  {'df':>5s}  ticker")
    cum = 0.0
    for i, (ticker, reward, target, df, _) in enumerate(rows[:80], start=1):
        cum += reward
        pct = cum / total_pool * 100
        print(f"  {i:3d}  ${reward:>6.0f}  {pct:>6.1f}%  {int(target):>6d}  {df:>5.2f}  {ticker[:40]}")

    # Cumulative cut-off
    print(f"\nCumulative pool captured by top-N:")
    for n in [10, 30, 50, 75, 100, 150, 200, 373]:
        if n <= len(rows):
            c = sum(r[1] for r in rows[:n])
            print(f"  top-{n:>3d}: ${c:>7.0f}/day ({c/total_pool*100:.1f}% of total)")

    # Reachable-target-size filter — at $80 bankroll, 2500-target markets are out of reach
    print(f"\n── Markets with TargetSize ≤ 500 (reachable at our capital) ──")
    reachable = [r for r in rows if r[2] <= 500]
    reachable_pool = sum(r[1] for r in reachable)
    print(f"  count: {len(reachable)}/{len(rows)} markets  "
          f"total: ${reachable_pool:,.0f}/day ({reachable_pool/total_pool*100:.1f}% of full pool)")
    print(f"  top-25 reachable:  ${sum(r[1] for r in reachable[:25]):>7.0f}/day")
    print(f"  top-50 reachable:  ${sum(r[1] for r in reachable[:50]):>7.0f}/day")
    print(f"  all reachable:     ${reachable_pool:>7.0f}/day")
    print(f"\n  Top-20 reachable markets:")
    for i, (ticker, reward, target, df, _) in enumerate(reachable[:20], start=1):
        print(f"    {i:2d}. ${reward:>5.0f}/day  ts={int(target):>4d}  df={df:.2f}  {ticker[:45]}")

    # By series prefix
    print(f"\nTop series by total reward pool:")
    by_series: dict[str, tuple[int, float]] = {}
    for ticker, reward, _, _, _ in rows:
        pfx = series_prefix(ticker)
        n, s = by_series.get(pfx, (0, 0.0))
        by_series[pfx] = (n + 1, s + reward)
    ranked = sorted(by_series.items(), key=lambda x: -x[1][1])
    for name, (n, pool) in ranked[:15]:
        print(f"  {name:30s}  {n:3d} markets   ${pool:>7.0f}/day")


if __name__ == "__main__":
    main()
