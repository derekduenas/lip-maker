"""Share-drift tracker — detect compression/recovery across live markets.

Paper-mode projections ran at 30-80% share; live reality is 20-25% once real
competitors react. This module answers: PER MARKET, is our share HOLDING,
COMPRESSING, or RECOVERING over time?

Uses hourly buckets from `lip_snapshots`:
  recent_6h_avg  = avg(share) in last 6h
  prior_6h_avg   = avg(share) in 6-12h ago
  trend_pct      = (recent - prior) / prior

Classifications:
  HOLDING     — |trend| < 10%
  COMPRESSING — trend < -10% (competitor presence grew)
  RECOVERING  — trend > +10% (competition thinned)
  VOLATILE    — stdev > 10 percentage points

Output feeds:
  - Dashboard per-market view
  - Rebate attribution (pre-Apr-28 payout prediction)
  - Basket quoter sizing decisions (#36 input)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

DB_PATH = settings.DB_PATH


def _hourly_shares(ticker: str, hours_back: int = 24,
                   db_path: str = DB_PATH) -> list[tuple[str, int, float]]:
    """Return [(hr_label, n_snaps, avg_share)] for a market, ordered chronologically."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT strftime('%Y-%m-%d %H', captured_at) AS hr,
                      COUNT(*) AS n,
                      AVG(CASE WHEN snapshot_valid=1 THEN our_score END)/2.0 AS share
               FROM lip_snapshots
               WHERE market_ticker = ?
                 AND datetime(captured_at) > datetime('now', ?)
               GROUP BY hr
               HAVING n >= 30
               ORDER BY hr""",
            (ticker, f"-{hours_back} hour"),
        ).fetchall()
        return [(r[0], r[1], (r[2] or 0)) for r in rows]
    finally:
        conn.close()


def analyze_market(ticker: str, db_path: str = DB_PATH) -> dict | None:
    """Return drift analysis for one market."""
    hourly = _hourly_shares(ticker, hours_back=24, db_path=db_path)
    if len(hourly) < 4:
        return None  # insufficient data

    shares = [h[2] for h in hourly]
    # Recent 6h
    recent = shares[-6:] if len(shares) >= 6 else shares
    # Prior 6h (the 6 buckets before `recent`)
    prior = shares[-12:-6] if len(shares) >= 12 else (shares[:-len(recent)] or [0])

    recent_avg = mean(recent) if recent else 0
    prior_avg  = mean(prior)  if prior  else 0
    trend_pct  = ((recent_avg - prior_avg) / prior_avg) if prior_avg > 0 else 0
    sd         = stdev(shares) if len(shares) > 1 else 0

    if sd > 0.10:
        classification = "VOLATILE"
    elif trend_pct < -0.10:
        classification = "COMPRESSING"
    elif trend_pct > 0.10:
        classification = "RECOVERING"
    else:
        classification = "HOLDING"

    return {
        "ticker":         ticker,
        "hours_observed": len(hourly),
        "recent_6h_share": round(recent_avg, 4),
        "prior_6h_share":  round(prior_avg,  4),
        "trend_pct":       round(trend_pct,  3),
        "stdev":           round(sd,         4),
        "classification":  classification,
        "hourly":          hourly,
    }


def _reward_for(ticker: str, db_path: str = DB_PATH) -> float:
    conn = sqlite3.connect(db_path)
    try:
        r = conn.execute(
            "SELECT reward_per_day_usd FROM lip_programs WHERE market_ticker = ?",
            (ticker,),
        ).fetchone()
        return float(r[0]) if r else 0.0
    finally:
        conn.close()


def attribution_table(min_snapshots: int = 120,
                      db_path: str = DB_PATH) -> list[dict]:
    """Per-market projected rebate + trend classification.

    Uses RECENT 6h share (live steady-state) not 24h average.
    """
    conn = sqlite3.connect(db_path)
    try:
        tickers = [r[0] for r in conn.execute(
            """SELECT market_ticker FROM lip_snapshots
               WHERE datetime(captured_at) > datetime('now', '-6 hour')
               GROUP BY market_ticker
               HAVING COUNT(*) >= ?""",
            (min_snapshots,),
        ).fetchall()]
    finally:
        conn.close()

    rows = []
    for t in tickers:
        a = analyze_market(t, db_path=db_path)
        if not a:
            continue
        reward = _reward_for(t, db_path=db_path)
        proj_day = a["recent_6h_share"] * reward
        rows.append({
            **a,
            "reward_per_day_usd": round(reward, 2),
            "projected_rebate":   round(proj_day, 2),
        })
    rows.sort(key=lambda r: -r["projected_rebate"])
    return rows


def summary(db_path: str = DB_PATH) -> dict:
    rows = attribution_table(db_path=db_path)
    by_class = defaultdict(list)
    for r in rows:
        by_class[r["classification"]].append(r)
    total_live = sum(r["projected_rebate"] for r in rows)
    compressing_loss = sum(
        (r["prior_6h_share"] - r["recent_6h_share"]) * r["reward_per_day_usd"]
        for r in rows if r["classification"] == "COMPRESSING"
    )
    return {
        "markets":           len(rows),
        "projected_daily":   round(total_live, 2),
        "by_class":          {k: len(v) for k, v in by_class.items()},
        "compressing_loss":  round(max(0, compressing_loss), 2),
        "top_compressing":   [r for r in rows if r["classification"] == "COMPRESSING"][:5],
        "top_recovering":    [r for r in rows if r["classification"] == "RECOVERING"][:5],
        "all":               rows,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", type=str, default=None, help="deep-dive on one market")
    p.add_argument("--top", type=int, default=20, help="how many to show")
    a = p.parse_args()

    if a.ticker:
        r = analyze_market(a.ticker)
        if not r:
            print(f"insufficient data for {a.ticker}")
            return
        print(f"\n{a.ticker}  [{r['classification']}]")
        print(f"  hours observed:  {r['hours_observed']}")
        print(f"  recent 6h share: {r['recent_6h_share']*100:.1f}%")
        print(f"  prior 6h share:  {r['prior_6h_share']*100:.1f}%")
        print(f"  trend:           {r['trend_pct']*100:+.1f}%")
        print(f"  stdev:           {r['stdev']*100:.1f}pp")
        print(f"\n  hourly trajectory:")
        for hr, n, share in r["hourly"]:
            bar = "█" * int(share * 100 / 2)
            print(f"    {hr}  n={n:>4}  share={share*100:>4.1f}%  {bar}")
        return

    s = summary()
    print(f"\n══ SHARE DRIFT TRACKER ══")
    print(f"  markets analyzed:        {s['markets']}")
    print(f"  by classification:       {dict(s['by_class'])}")
    print(f"  PROJECTED DAILY REBATE:  ${s['projected_daily']:,.2f}/day  (live steady-state)")
    if s["compressing_loss"] > 0:
        print(f"  ⚠ compression cost:     ${s['compressing_loss']:.2f}/day (vs prior 6h)")

    print(f"\n  🔴 TOP COMPRESSING (share shrinking — competitor entered):")
    for r in s["top_compressing"][:a.top//2]:
        print(f"    {r['ticker'][:36]:<38s}  "
              f"{r['prior_6h_share']*100:>4.1f}% → {r['recent_6h_share']*100:>4.1f}%  "
              f"({r['trend_pct']*100:+.0f}%)  "
              f"proj=${r['projected_rebate']:>5.2f}")

    print(f"\n  🟢 TOP RECOVERING (share growing — competitor left):")
    for r in s["top_recovering"][:a.top//2]:
        print(f"    {r['ticker'][:36]:<38s}  "
              f"{r['prior_6h_share']*100:>4.1f}% → {r['recent_6h_share']*100:>4.1f}%  "
              f"({r['trend_pct']*100:+.0f}%)  "
              f"proj=${r['projected_rebate']:>5.2f}")

    print(f"\n  📊 TOP HOLDING (stable — the load-bearing earners):")
    holding = [r for r in s["all"] if r["classification"] == "HOLDING"]
    for r in holding[:a.top//2]:
        print(f"    {r['ticker'][:36]:<38s}  "
              f"share={r['recent_6h_share']*100:>4.1f}%  "
              f"stdev={r['stdev']*100:>3.1f}pp  "
              f"proj=${r['projected_rebate']:>5.2f}")


if __name__ == "__main__":
    main()
