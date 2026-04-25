"""Rebate payout predictor + reconciler.

Kalshi pays LIP weekly (Monday-Sunday). First real payout for our live run:
Apr 28, 2026. This tool:

  1. For each market we've snapshotted this week, predict our rebate using:
       prediction = sum(our_score_per_snapshot) / (2 × N_snapshots) × reward_per_day × days_active

  2. Aggregate across all markets → predicted total weekly payout.

  3. When Apr 28 payout hits, reconcile: actual vs predicted per-market + total.

This converts the Apr 28 number from "a mystery payment" into "calibration
signal." Markets that pay MORE than predicted → we've been underquoting/
competitors are thinner than we think. Markets that pay LESS → share formula
is off, OR we were offline at critical snapshots.

Output:
  - Pre-Apr-28: prediction table (what we expect)
  - Post-Apr-28: reconciliation table (actual vs predicted, variance per market)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)


# Live-flip moment — paper-mode data before this is NOT representative of
# competitive dynamics. Predictions use max(monday_00:00, LIVE_FLIP).
LIVE_FLIP_UTC = datetime(2026, 4, 21, 1, 48, tzinfo=timezone.utc)

# 2026-04-25 PHANTOM-SNAPSHOT FIX deploy time. Snapshots before this time
# do NOT have honest was_resting flags (scorer simulated "as if resting").
# Predictor MUST exclude pre-fix data or share calculations are corrupted.
PHANTOM_FIX_DEPLOY_UTC = datetime(2026, 4, 25, 18, 14, 51, tzinfo=timezone.utc)


def _week_window(anchor_day: datetime | None = None) -> tuple[datetime, datetime]:
    """Return (effective_start, next_monday_00:00_utc) for current LIP week.

    Kalshi LIP week: Monday 00:00 UTC through Sunday 23:59:59 UTC.
    effective_start = max(monday_00:00, LIVE_FLIP_UTC, PHANTOM_FIX_DEPLOY_UTC)
    — avoids both paper-mode data pollution AND pre-phantom-fix noise.
    """
    today = anchor_day or datetime.now(timezone.utc)
    monday = today - timedelta(days=today.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    effective_start = max(monday, LIVE_FLIP_UTC, PHANTOM_FIX_DEPLOY_UTC)
    return effective_start, monday + timedelta(days=7)


def predict_week(week_start: datetime | None = None,
                 db_path: str = settings.DB_PATH) -> dict:
    """Predict rebate for the week containing `week_start` (default: current)."""
    start, end = _week_window(week_start)
    now = datetime.now(timezone.utc)
    effective_end = min(now, end)  # don't project past "now" for ongoing weeks

    # 2026-04-25 PHANTOM-FIX: filter our_score on was_resting=1 AND snapshot_valid=1.
    # Pre-fix snapshots have was_resting=0 (default from migration) so historical
    # phantom data is correctly excluded. Post-fix snapshots reflect honest scoring.
    # n_snaps stays as TOTAL observations — share is "avg over time" including
    # 0-earning phantom windows, which is mathematically correct for payout prediction.
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            """SELECT s.market_ticker,
                      COUNT(*)                                      AS n_snaps,
                      SUM(CASE WHEN s.snapshot_valid=1 AND s.was_resting=1
                               THEN s.our_score ELSE 0 END)         AS our_score_sum,
                      SUM(CASE WHEN s.snapshot_valid=1 AND s.was_resting=1
                               THEN 1 ELSE 0 END)                   AS valid_n,
                      SUM(CASE WHEN s.was_resting=1 THEN 1 ELSE 0 END) AS resting_n,
                      p.reward_per_day_usd,
                      p.target_size
               FROM lip_snapshots s
               LEFT JOIN lip_programs p ON s.market_ticker = p.market_ticker
               WHERE s.captured_at >= ? AND s.captured_at < ?
                 AND p.enrolled = 1
               GROUP BY s.market_ticker
               HAVING n_snaps >= 30""",
            (start.isoformat(), effective_end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    # Estimate "snapshots per day" from actual sample density
    window_hours = (effective_end - start).total_seconds() / 3600
    if window_hours < 0.5:
        return {"error": "week just started — insufficient data"}

    # Per-market: pessimistic estimator share = our_score_sum / (2 × n_snaps)
    # Then payout this far = share × reward_per_day × (hours_so_far / 24)
    # 2026-04-22: Apply $1.00 per-market floor per Kalshi LIP docs:
    #   "if the result is greater than or equal to $1.00, the result is paid out".
    # Sub-$1 markets earn ZERO. Surfaces lost-to-floor as separate metric.
    PER_MARKET_FLOOR_USD = 1.00
    predictions = []
    total_predicted = 0.0
    total_lost_to_floor = 0.0
    total_resting_n = 0
    total_n_snaps = 0
    n_floored = 0
    for tkr, n, our_sum, valid_n, resting_n, reward, target in rows:
        if not n or not reward:
            continue
        share = (our_sum or 0) / (2.0 * n) if n > 0 else 0
        hours_so_far = window_hours
        realized_to_date_raw = share * reward * (hours_so_far / 24)
        # 2026-04-25: project from honest-data window (post phantom-fix)
        # forward through end of week. Pre-honest hours produce $0 (we treat
        # them as "we weren't live" for projection purposes).
        monday_00 = end - timedelta(days=7)
        hours_pre_honest = max(0, (start - monday_00).total_seconds() / 3600)
        hours_honest_total = 168 - hours_pre_honest
        full_week_raw = share * reward * (hours_honest_total / 24)

        # Apply $1.00 floor (round down to nearest cent per docs)
        if full_week_raw < PER_MARKET_FLOOR_USD:
            full_week = 0.0
            realized_to_date = 0.0
            total_lost_to_floor += full_week_raw
            n_floored += 1
        else:
            full_week = int(full_week_raw * 100) / 100.0  # round down to cent
            realized_to_date = realized_to_date_raw
        total_predicted += full_week
        total_resting_n += (resting_n or 0)
        total_n_snaps += n
        predictions.append({
            "ticker":            tkr,
            "n_snaps":           n,
            "resting_n":         resting_n or 0,
            "valid_n":           valid_n or 0,
            "resting_pct":       round(100.0 * (resting_n or 0) / n, 1) if n else 0,
            "share_pct":         round(share * 100, 2),
            "reward_per_day":    reward,
            "realized_to_date":  round(realized_to_date, 2),
            "full_week_pred":    round(full_week, 2),
        })
    predictions.sort(key=lambda x: -x["full_week_pred"])

    overall_resting_pct = round(100.0 * total_resting_n / total_n_snaps, 1) if total_n_snaps else 0

    return {
        "week_start":      start.isoformat(),
        "week_end":        end.isoformat(),
        "hours_observed":  round(window_hours, 1),
        "markets_active":  len(predictions),
        "markets_floored": n_floored,
        "lost_to_floor":   round(total_lost_to_floor, 2),
        "total_resting_pct": overall_resting_pct,
        "total_n_snaps":   total_n_snaps,
        "total_resting_n": total_resting_n,
        "total_realized_to_date": round(sum(p["realized_to_date"] for p in predictions), 2),
        "total_weekly_predicted": round(total_predicted, 2),
        "per_market":      predictions,
    }


def reconcile(actual_per_market: dict[str, float],
              week_start: datetime | None = None,
              db_path: str = settings.DB_PATH) -> dict:
    """Compare Kalshi's actual payout to our prediction.

    `actual_per_market` is a dict of {ticker: actual_usd_paid}.
    """
    pred = predict_week(week_start, db_path=db_path)
    by_ticker = {p["ticker"]: p for p in pred["per_market"]}

    rows = []
    total_predicted = 0.0
    total_actual = 0.0
    for ticker, p in by_ticker.items():
        predicted = p["full_week_pred"]
        actual = actual_per_market.get(ticker, 0)
        variance = actual - predicted
        variance_pct = (variance / predicted) if predicted > 0 else 0
        total_predicted += predicted
        total_actual += actual
        rows.append({
            "ticker":       ticker,
            "predicted":    predicted,
            "actual":       actual,
            "variance":     round(variance, 2),
            "variance_pct": round(variance_pct, 3),
        })

    rows.sort(key=lambda r: -abs(r["variance"]))
    return {
        "total_predicted":  round(total_predicted, 2),
        "total_actual":     round(total_actual, 2),
        "overall_variance": round(total_actual - total_predicted, 2),
        "overall_ratio":    round(total_actual / total_predicted, 3) if total_predicted else 0,
        "per_market":       rows,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--week", type=str, default=None,
                   help="ISO date in the week to analyze (default: current)")
    p.add_argument("--top", type=int, default=15)
    a = p.parse_args()

    anchor = datetime.fromisoformat(a.week).replace(tzinfo=timezone.utc) if a.week else None
    r = predict_week(week_start=anchor)

    if "error" in r:
        print(f"  {r['error']}")
        return

    print(f"\n══ REBATE PREDICTOR (PHANTOM-CORRECTED) ══")
    print(f"  week:              {r['week_start'][:10]} → {r['week_end'][:10]}")
    print(f"  hours observed:    {r['hours_observed']:.1f} of 168")
    print(f"  markets active:    {r['markets_active']}")
    print(f"  resting rate:      {r['total_resting_pct']}% "
          f"({r['total_resting_n']:,} of {r['total_n_snaps']:,} snaps were truly resting)")
    print(f"  REALIZED to date:  ${r['total_realized_to_date']:,.2f}")
    print(f"  PROJECTED full wk: ${r['total_weekly_predicted']:,.2f}   "
          f"(at Apr 28 payout — ground truth)")
    print(f"\n  ⚠️  Pre-2026-04-25 18:14 snapshots are excluded (phantom-flagged).")
    print(f"     Numbers will become more accurate as honest data accumulates.")

    print(f"\n  Top {a.top} contributors (projected full week):")
    for e in r["per_market"][:a.top]:
        print(f"    {e['ticker'][:36]:<38s}  "
              f"resting={e['resting_pct']:>4.1f}%  "
              f"share={e['share_pct']:>4.1f}%  "
              f"reward=${e['reward_per_day']:>4.0f}  "
              f"realized=${e['realized_to_date']:>5.2f}  "
              f"pred=${e['full_week_pred']:>6.2f}")


if __name__ == "__main__":
    main()
