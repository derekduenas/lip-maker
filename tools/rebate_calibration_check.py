"""Scanner ↔ Kalshi UI rebate reconciler (#118).

PURPOSE:
  Our settlement_reconciler estimates rebate from lip_snapshots data
  (#120), calibrated to ~85% of Kalshi UI ground truth. Over time the
  multiplier drifts as snapshot density changes (more book-driven snaps
  = lower effective per-snapshot interval). This tool compares our
  estimates to user-supplied Kalshi UI numbers, flags drift, and
  recommends multiplier adjustments.

USAGE:
  # Show current calibration vs known ground truth (Apr 21-26 baseline):
  python tools/rebate_calibration_check.py

  # Add new ground-truth observation from Kalshi UI:
  python tools/rebate_calibration_check.py --add KXCORNW=42.50 KXBRENTD=14.00

  # Recompute multiplier suggestion:
  python tools/rebate_calibration_check.py --suggest-multiplier
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


# Apr 21-26 frozen baseline (from project_full_trading_audit_apr27.md +
# tools/calibrated_predictor.py KALSHI_PAID_BY_SERIES). Each value is
# the ACTUAL Kalshi-paid rebate over Apr 21-26 (~6 days).
GROUND_TRUTH_APR_21_26 = {
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
    "KXLITHIUMW":            5.75,
    "KXLCATTLEW":            8.00,
    "KXSOYBEANW":            1.18,
    "KXNATGASD":             5.20,
    "KXNATGASW":             1.51,
    "KXTRUMPENDORSEMENTS":   4.20,
    "KXGENERICBALLOTVOTEHUB": 1.93,
}

CALIBRATION_FILE = Path(settings.DATA_DIR) / "rebate_calibration.json"


def load_observations() -> dict:
    """Load user-added ground-truth observations from disk."""
    if CALIBRATION_FILE.exists():
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except Exception:
            return {"observations": [], "current_multiplier": 30.0}
    return {"observations": [], "current_multiplier": 30.0}


def save_observations(data: dict) -> None:
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_FILE.write_text(json.dumps(data, indent=2))


def our_estimate_per_series(window_start: str, window_end: str,
                            db_path: str = settings.DB_PATH) -> dict:
    """Sum settlement_log.rebate_earned_usd by series in time window."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            """SELECT series_prefix,
                      ROUND(SUM(COALESCE(rebate_earned_usd, 0)), 2) AS rebate
               FROM settlement_log
               WHERE close_time >= ? AND close_time < ?
               GROUP BY series_prefix
               ORDER BY rebate DESC""",
            (window_start, window_end),
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: float(r[1] or 0) for r in rows}


def calibration_report(observations: dict | None = None) -> dict:
    """Compare our estimates against ground truth, return calibration stats."""
    if observations is None:
        # Use the frozen baseline
        truth = GROUND_TRUTH_APR_21_26
        window_start = "2026-04-21T00:00:00"
        window_end   = "2026-04-27T00:00:00"
        label = "Apr 21-26 baseline"
    else:
        truth = observations
        window_start = ""
        window_end   = ""
        label = "user observations"

    estimates = our_estimate_per_series(window_start, window_end) if window_start else {}

    rows = []
    total_est = 0.0
    total_truth = 0.0
    for series, actual in sorted(truth.items()):
        est = estimates.get(series, 0.0)
        total_est += est
        total_truth += actual
        if est > 0:
            ratio = actual / est
            drift_pct = (ratio - 1.0) * 100
        else:
            ratio = None
            drift_pct = None
        rows.append({
            "series": series,
            "estimate":  round(est, 2),
            "actual":    round(actual, 2),
            "ratio":     round(ratio, 3) if ratio is not None else None,
            "drift_pct": round(drift_pct, 1) if drift_pct is not None else None,
        })

    overall_ratio = total_truth / total_est if total_est > 0 else None
    suggested_multiplier = settings_multiplier() * overall_ratio if overall_ratio else None

    return {
        "label":          label,
        "rows":           rows,
        "total_estimate": round(total_est, 2),
        "total_actual":   round(total_truth, 2),
        "overall_ratio":  round(overall_ratio, 3) if overall_ratio else None,
        "current_multiplier": settings_multiplier(),
        "suggested_multiplier": round(suggested_multiplier, 1) if suggested_multiplier else None,
    }


def settings_multiplier() -> float:
    """Read SNAPSHOT_REPRESENTS_SECONDS from settlement_reconciler."""
    try:
        from tools.settlement_reconciler import SNAPSHOT_REPRESENTS_SECONDS
        return float(SNAPSHOT_REPRESENTS_SECONDS)
    except Exception:
        return 30.0


def print_report(report: dict) -> None:
    print(f"\n══ REBATE CALIBRATION REPORT — {report['label']} ══\n")
    print(f"  {'series':<28s}  {'estimate':>10s}  {'actual':>10s}  {'ratio':>7s}  {'drift':>8s}")
    print(f"  {'-'*28}  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*8}")
    for r in report["rows"]:
        ratio_str = f"{r['ratio']:>6.3f}x" if r['ratio'] is not None else "    n/a"
        drift_str = f"{r['drift_pct']:>+6.1f}%" if r['drift_pct'] is not None else "    n/a"
        print(f"  {r['series']:<28s}  ${r['estimate']:>9.2f}  ${r['actual']:>9.2f}  "
              f"{ratio_str}  {drift_str}")
    print(f"  {'-'*28}  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*8}")
    print(f"  {'TOTAL':<28s}  ${report['total_estimate']:>9.2f}  "
          f"${report['total_actual']:>9.2f}  "
          f"{(report['overall_ratio'] or 0):>6.3f}x")
    print()
    print(f"  Current SNAPSHOT_REPRESENTS_SECONDS = {report['current_multiplier']}")
    if report["suggested_multiplier"]:
        delta = report["suggested_multiplier"] - report["current_multiplier"]
        if abs(delta) >= 2:
            print(f"  💡 SUGGESTED multiplier = {report['suggested_multiplier']} "
                  f"(delta {delta:+.1f}). Edit tools/settlement_reconciler.py "
                  f"and re-run --backfill --force.")
        else:
            print(f"  ✓ Calibration within ±2 of suggested ({report['suggested_multiplier']}). "
                  f"No tuning needed.")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--add", nargs="+", default=[],
                   help="Add ground-truth observations: SERIES=AMOUNT [SERIES=AMOUNT ...]")
    p.add_argument("--baseline", action="store_true",
                   help="Use frozen Apr 21-26 baseline only (default)")
    p.add_argument("--show-saved", action="store_true",
                   help="Show saved user observations")
    a = p.parse_args()

    if a.add:
        data = load_observations()
        new_obs = {}
        for kv in a.add:
            if "=" not in kv:
                print(f"WARN: skipping malformed {kv} (use SERIES=AMOUNT)")
                continue
            k, v = kv.split("=", 1)
            try:
                new_obs[k.strip()] = float(v)
            except ValueError:
                print(f"WARN: skipping {kv} — not a number")
        if new_obs:
            data["observations"].append({
                "added_at": datetime.now(timezone.utc).isoformat(),
                "data":     new_obs,
            })
            save_observations(data)
            print(f"Added {len(new_obs)} observations to {CALIBRATION_FILE}")
        return 0

    if a.show_saved:
        data = load_observations()
        print(f"Saved observations ({len(data.get('observations', []))} entries):")
        for entry in data.get("observations", []):
            print(f"  {entry['added_at'][:19]}: {entry['data']}")
        return 0

    # Default: show baseline calibration report
    report = calibration_report()
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
