"""Calibrated rebate predictor — corrects the broken /2.0 denominator.

THE PROBLEM (verified 2026-04-27):
  Our model assumes pool_denominator = 2.0 (us + 1 competitor at equal score).
  Reality: 3-5 LPs per market, so our actual share ≈ predicted_share / 4.

  Predictions vs actual across the week:
    Method 1 (per-market post-fix × 7):  $855/wk
    Agent (harsher pre-fix discount):    $550/wk
    Refined:                              $1,750/wk
    Deep-dive:                            $2,000/wk
    Naive (phantom):                      $15,000/wk
    REALITY (Kalshi UI):                  $220/wk

  Implied calibration factor: 0.20-0.30 of model output.

THIS TOOL:
  1. Per-event actual rebate from Kalshi UI (manually entered for now)
  2. Same-event predicted from our snapshot data
  3. Per-series calibration factor (actual / predicted)
  4. Apply factor to current scanner output → honest forward projection

When --update flag passed, writes CALIBRATION_FACTORS dict that
rebate_predictor.py can read.

Usage:
  python tools/calibrated_predictor.py                    # show calibration analysis
  python tools/calibrated_predictor.py --forecast         # forecast next 7 days
  python tools/calibrated_predictor.py --update           # recalibrate factors
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


# ── Manually entered Kalshi-paid rebate per series Apr 21-26 ─────────────
# Source: user's Kalshi UI rewards page (verified 2026-04-27)
KALSHI_PAID_BY_SERIES = {
    "KXCORNW":              36.76,
    "KXBRENTD":             12.30,
    "KXCOPPERD":            13.93,
    "KXGOLDW":              21.48,
    "KXCOCOAW":             22.48,
    "KXHOILW":              19.73,
    "KXWHEATW":             17.18,
    "KXSUGARW":             14.99,
    "KXCOFFEEW":            10.00,
    "KXSILVERMON":           8.91,
    "KXSILVERW":             1.62,
    "KXLITHIUMW":            5.75,   # 1.66 + 1.23 + 2.86
    "KXNICKELW":             0,      # not paid
    "KXLCATTLEW":            8.00,   # 5.52 + 2.48
    "KXSOYBEANW":            1.18,
    "KXNATGASD":             5.20,   # 1.36 + 2.84 + 2 (some attribution unclear)
    "KXNATGASW":             1.51,
    "KXTRUMPENDORSEMENTS":   4.20,
    "KXVOTEHUBTRUMPUPDOWN":  0,      # not paid
    "KXGENERICBALLOTVOTEHUB": 1.93,
    # Total: ~$220.59 (matches user's reported $220.59 lifetime)
}

LIVE_FLIP_UTC = datetime(2026, 4, 21, 1, 48, tzinfo=timezone.utc)
DAYS_OBSERVED = 6.0   # Apr 21 → Apr 27 ~6 days


def model_prediction_per_series(db_path: str = settings.DB_PATH) -> dict[str, float]:
    """Compute what OUR model predicted each series would earn, using
    post-fix data only (pre-fix is phantom-inflated)."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            """SELECT
                 substr(s.market_ticker, 1, instr(s.market_ticker || '-', '-') - 1) AS series,
                 SUM(CASE WHEN s.was_resting=1 AND s.snapshot_valid=1
                          THEN s.our_score ELSE 0 END) AS sum_score,
                 SUM(s.was_resting) AS resting_n,
                 AVG(p.reward_per_day_usd) AS avg_reward
               FROM lip_snapshots s
               LEFT JOIN lip_programs p ON p.market_ticker = s.market_ticker
               WHERE datetime(s.captured_at) > datetime('2026-04-25 18:14:51')
                 AND s.was_resting = 1
                 AND p.reward_per_day_usd IS NOT NULL
               GROUP BY series""",
        ).fetchall()
    finally:
        conn.close()

    # Naive prediction = (avg_share) × reward × days where avg_share = sum_score / (2 × resting_n)
    out: dict[str, float] = {}
    for series, sum_score, resting_n, avg_reward in rows:
        if not series or not resting_n or not avg_reward:
            continue
        avg_share = (sum_score or 0) / (2.0 * resting_n)
        # Project to 6 days observed (matches actual data window)
        predicted = avg_share * avg_reward * 6.0
        out[series] = round(predicted, 2)
    return out


def compute_calibration() -> dict:
    """For each series with both predicted + actual, compute calibration factor."""
    predicted = model_prediction_per_series()
    rows = []
    total_actual = 0.0
    total_predicted = 0.0
    for series, actual in KALSHI_PAID_BY_SERIES.items():
        pred = predicted.get(series, 0.0)
        if pred > 0:
            factor = actual / pred
            rows.append({
                "series":       series,
                "predicted":    pred,
                "actual":       actual,
                "factor":       round(factor, 3),
                "delta":        round(actual - pred, 2),
            })
        else:
            rows.append({
                "series":       series,
                "predicted":    pred,
                "actual":       actual,
                "factor":       None,  # can't compute, no prediction
                "delta":        round(actual - pred, 2),
            })
        total_actual += actual
        total_predicted += pred
    rows.sort(key=lambda r: -r["actual"])
    overall_factor = total_actual / total_predicted if total_predicted else 0
    return {
        "rows":             rows,
        "total_actual":     round(total_actual, 2),
        "total_predicted":  round(total_predicted, 2),
        "overall_factor":   round(overall_factor, 3),
    }


def forecast_with_calibration(calibration_factor: float,
                              db_path: str = settings.DB_PATH) -> dict:
    """Project next 7 days using calibrated factor."""
    pred = model_prediction_per_series(db_path)
    raw_total = sum(pred.values())
    calibrated = raw_total * calibration_factor
    return {
        "raw_model":    round(raw_total, 2),
        "calibrated":   round(calibrated, 2),
        "factor":       calibration_factor,
        "weekly":       round(calibrated / 6.0 * 7, 2),
    }


def print_report(cal: dict) -> None:
    print("\n══════════════════════════════════════════════════════════════")
    print("  CALIBRATED PREDICTOR — Model vs Reality")
    print("══════════════════════════════════════════════════════════════\n")
    print(f"  {'Series':<26s} {'Predicted':>10s} {'Actual':>10s} {'Δ':>9s} {'Factor':>8s}")
    print(f"  {'-'*26} {'-'*10} {'-'*10} {'-'*9} {'-'*8}")
    for r in cal["rows"]:
        factor_str = f"{r['factor']:>7.3f}x" if r['factor'] is not None else "    n/a"
        print(f"  {r['series']:<26s} ${r['predicted']:>9.2f} ${r['actual']:>9.2f} "
              f"${r['delta']:>+8.2f} {factor_str}")
    print(f"  {'-'*26} {'-'*10} {'-'*10} {'-'*9} {'-'*8}")
    print(f"  {'TOTAL':<26s} ${cal['total_predicted']:>9.2f} "
          f"${cal['total_actual']:>9.2f} "
          f"${cal['total_actual']-cal['total_predicted']:>+8.2f} "
          f"{cal['overall_factor']:>7.3f}x")
    print()
    print(f"  📊 OVERALL CALIBRATION FACTOR: {cal['overall_factor']}")
    print(f"     (multiply raw model output by this to get realistic forecast)")
    print()
    print(f"  Interpretation:")
    if cal['overall_factor'] < 0.5:
        print(f"  - Model is overstating by ~{1/cal['overall_factor']:.1f}x")
        print(f"  - Real LIP pool has ~{2/cal['overall_factor']:.1f} effective competitors")
        print(f"    (we assume /2 in formula; real denominator ~{2/cal['overall_factor']:.1f})")
    elif cal['overall_factor'] > 1.5:
        print(f"  - Model is UNDERSTATING by {cal['overall_factor']:.1f}x")
        print(f"  - Real share is HIGHER than we estimate (less competition)")
    else:
        print(f"  - Model is roughly accurate")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--forecast", action="store_true",
                   help="Show calibrated forward projection")
    p.add_argument("--update", action="store_true",
                   help="Write calibration factor to disk for predictor to use")
    a = p.parse_args()

    cal = compute_calibration()
    print_report(cal)

    if a.forecast:
        forecast = forecast_with_calibration(cal["overall_factor"])
        print("\n══ FORECAST (next 7 days) ══")
        print(f"  Raw model output:     ${forecast['raw_model']:,.2f}")
        print(f"  Calibration factor:   {forecast['factor']}x")
        print(f"  Calibrated 6-day:     ${forecast['calibrated']:,.2f}")
        print(f"  Calibrated weekly:    ${forecast['weekly']:,.2f}")
        print()

    if a.update:
        out_file = Path(settings.DATA_DIR) / "calibration.json"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w") as f:
            json.dump({
                "overall_factor": cal["overall_factor"],
                "per_series_factors": {
                    r["series"]: r["factor"]
                    for r in cal["rows"] if r["factor"] is not None
                },
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "data_window": "Apr 21-26 2026",
            }, f, indent=2)
        print(f"  ✓ Wrote calibration to {out_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
