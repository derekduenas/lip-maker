"""Apr 28 (and beyond) weekly LIP payout reconciliation.

Run after Kalshi posts the weekly LIP payout. Compares actual against:
  1. Naive predictor pre phantom-fix (the FANTASY number we want to disprove)
  2. Deep-dive locked estimate ($2,000, band $1,700-$2,400)
  3. Refined post-Apr-24 estimate ($1,500-$2,500, mid $1,750)
  4. Phantom-corrected forward predictor (computed live)

Then matches actual to the pre-decided injection ladder and prints the
recommended action. Saves the result to apr28_reconciliation_log table for
historical tracking week-over-week.

Usage:
  # Required: actual total payout from Kalshi LIP dashboard
  python tools/apr28_reconcile.py --actual 1850.00

  # Optional: per-market CSV (ticker,actual_usd) for variance analysis
  python tools/apr28_reconcile.py --actual 1850.00 --per-market actuals.csv

  # Re-run for a past week
  python tools/apr28_reconcile.py --actual 1850.00 --anchor 2026-04-25
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from tools.rebate_predictor import predict_week

_log = logging.getLogger(__name__)


# ── Frozen predictions (locked 2026-04-24 to 2026-04-26) ──────────────────
# These are the SPECIFIC numbers we committed to before Apr 28 lands.
# DO NOT update these post-result — that defeats the purpose of a frozen
# baseline. Add new entries to PREDICTION_HISTORY for future weeks instead.

@dataclass(frozen=True)
class FrozenPrediction:
    name: str
    point: float
    band_low: float | None
    band_high: float | None
    locked_date: str
    notes: str


FROZEN_BASELINES_2026_04_28 = [
    FrozenPrediction(
        name="naive_predictor_pre_fix",
        point=15360.0,
        band_low=None, band_high=None,
        locked_date="2026-04-24",
        notes="Phantom-inflated. Expected to be WAY off. Confirms phantom-bug hypothesis if huge gap.",
    ),
    FrozenPrediction(
        name="hand_wave_84pct_haircut",
        point=2458.0,
        band_low=2500.0, band_high=5000.0,
        locked_date="2026-04-24",
        notes="My initial single-sample correction. Probably high.",
    ),
    FrozenPrediction(
        name="deep_dive_locked",
        point=2000.0,
        band_low=1700.0, band_high=2400.0,
        locked_date="2026-04-24",
        notes="Rigorous methodology: 73h data, multi-window phantom rate, capacity arithmetic.",
    ),
    FrozenPrediction(
        name="refined_post_apr24_incident",
        point=1750.0,
        band_low=1500.0, band_high=2500.0,
        locked_date="2026-04-26",
        notes="Slightly lower after Apr 24 -$90 incident + cascade drag.",
    ),
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS apr28_reconciliation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start      TEXT NOT NULL,
    week_end        TEXT NOT NULL,
    actual_total    REAL NOT NULL,
    naive_predicted REAL,
    deep_dive_predicted REAL,
    refined_predicted REAL,
    phantom_corrected_predicted REAL,
    closest_source  TEXT,
    closest_variance_pct REAL,
    injection_tier  TEXT,
    injection_amount REAL,
    recorded_at     TEXT NOT NULL,
    UNIQUE(week_start)
);
"""


# ── Injection ladder (locked memory: project_apr28_action_plan.md) ──────

@dataclass(frozen=True)
class InjectionTier:
    label: str
    floor: float        # actual >= floor
    ceiling: float      # actual < ceiling (or float('inf'))
    inject_usd: float
    phase_action: str


# 2026-04-26 REVISED: smaller inject + skip-to-P4. Compounding-dominated math.
# See memory/project_apr28_action_plan.md for full reasoning.
INJECTION_LADDER = [
    InjectionTier("NEGATIVE", float("-inf"), 0.0,
                  0.0, "HALT + audit"),
    InjectionTier("DIAGNOSE_FIRST", 0.0, 1500.0,
                  0.0, "Stay Phase 1 — diagnose"),
    InjectionTier("CONFIRM_BASELINE", 1500.0, 2500.0,
                  5000.0, "Skip directly to Phase 4 (smaller inject, compounding will catch up)"),
    InjectionTier("STRONG_VALIDATION", 2500.0, 4000.0,
                  10000.0, "Skip directly to Phase 4"),
    InjectionTier("MASSIVE_UPSIDE", 4000.0, float("inf"),
                  15000.0, "Phase 4 immediately, save $10k for week-2 re-inject if data confirms"),
]


def find_tier(actual: float) -> InjectionTier:
    for tier in INJECTION_LADDER:
        if tier.floor <= actual < tier.ceiling:
            return tier
    raise ValueError(f"actual ${actual} matched no tier — ladder bug")


# ── Reconciliation ──────────────────────────────────────────────────────

@dataclass
class Comparison:
    source: str
    predicted: float
    actual: float
    variance: float          # actual - predicted
    variance_pct: float      # (actual - predicted) / predicted
    in_band: bool            # if predicted has a band, did actual land in it?
    notes: str


def compare_predictions(
    actual: float,
    baselines: list[FrozenPrediction],
    live_phantom_corrected: float,
) -> list[Comparison]:
    out: list[Comparison] = []
    for b in baselines:
        var = actual - b.point
        var_pct = var / b.point if b.point else 0.0
        in_band = (
            b.band_low is not None and b.band_high is not None
            and b.band_low <= actual <= b.band_high
        )
        out.append(Comparison(
            source=b.name,
            predicted=b.point,
            actual=actual,
            variance=round(var, 2),
            variance_pct=round(var_pct, 3),
            in_band=in_band,
            notes=b.notes,
        ))
    # Add live phantom-corrected as an extra comparison
    var = actual - live_phantom_corrected
    var_pct = var / live_phantom_corrected if live_phantom_corrected else 0.0
    out.append(Comparison(
        source="phantom_corrected_forward",
        predicted=live_phantom_corrected,
        actual=actual,
        variance=round(var, 2),
        variance_pct=round(var_pct, 3),
        in_band=False,
        notes="Live predictor at Apr 28 (post-phantom-fix data only).",
    ))
    return out


def find_closest(comps: list[Comparison]) -> Comparison:
    return min(comps, key=lambda c: abs(c.variance))


def load_per_market_actuals(path: str) -> dict[str, float]:
    actuals: dict[str, float] = {}
    with open(path) as f:
        reader = csv.reader(f)
        first = next(reader, None)
        # Allow header or no-header
        if first and not first[0].replace(".", "").isdigit() and "," in (first[0] + (first[1] if len(first) > 1 else "")):
            try:
                float(first[1])
                actuals[first[0]] = float(first[1])
            except (ValueError, IndexError):
                pass  # was a header
        for row in reader:
            if len(row) < 2:
                continue
            try:
                actuals[row[0]] = float(row[1])
            except ValueError:
                continue
    return actuals


def per_market_reconciliation(
    actual_per_market: dict[str, float],
    predicted_per_market: list[dict],
) -> list[dict]:
    """Combine actual + predicted for each market, sort by abs variance."""
    by_ticker = {p["ticker"]: p for p in predicted_per_market}
    rows = []
    for ticker, actual in actual_per_market.items():
        pred = by_ticker.get(ticker, {})
        predicted = pred.get("full_week_pred", 0.0)
        rows.append({
            "ticker": ticker,
            "predicted": predicted,
            "actual": actual,
            "variance": round(actual - predicted, 2),
            "variance_pct": round(((actual - predicted) / predicted), 3) if predicted else None,
        })
    # Also add markets we predicted but no actual reported (could be sub-$1 floor or missing)
    for ticker, pred in by_ticker.items():
        if ticker not in actual_per_market:
            rows.append({
                "ticker": ticker,
                "predicted": pred.get("full_week_pred", 0.0),
                "actual": 0.0,
                "variance": round(-pred.get("full_week_pred", 0.0), 2),
                "variance_pct": -1.0,  # missed entirely
            })
    rows.sort(key=lambda r: -abs(r["variance"]))
    return rows


def persist_reconciliation(
    week_start: str,
    week_end: str,
    actual: float,
    comps: list[Comparison],
    closest: Comparison,
    tier: InjectionTier,
    db_path: str = settings.DB_PATH,
) -> None:
    by_source = {c.source: c.predicted for c in comps}
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            """INSERT OR REPLACE INTO apr28_reconciliation_log
               (week_start, week_end, actual_total,
                naive_predicted, deep_dive_predicted, refined_predicted,
                phantom_corrected_predicted,
                closest_source, closest_variance_pct,
                injection_tier, injection_amount, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                week_start, week_end, actual,
                by_source.get("naive_predictor_pre_fix"),
                by_source.get("deep_dive_locked"),
                by_source.get("refined_post_apr24_incident"),
                by_source.get("phantom_corrected_forward"),
                closest.source, closest.variance_pct,
                tier.label, tier.inject_usd,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── Pretty printing ────────────────────────────────────────────────────

def print_report(
    actual: float,
    week_start: str,
    week_end: str,
    comps: list[Comparison],
    closest: Comparison,
    tier: InjectionTier,
    per_market_rows: list[dict] | None,
    pred_total_resting_pct: float | None,
) -> None:
    print()
    print("══════════════════════════════════════════════════════════════")
    print(f"  APR 28 LIP PAYOUT RECONCILIATION")
    print("══════════════════════════════════════════════════════════════")
    print(f"  Week:           {week_start[:10]} → {week_end[:10]}")
    print(f"  ACTUAL PAYOUT:  ${actual:,.2f}")
    if pred_total_resting_pct is not None:
        print(f"  Resting rate:   {pred_total_resting_pct}% (post phantom-fix)")
    print()
    print("  ── Predictions vs Actual ──────────────────────────────────")
    print(f"  {'Source':<35s} {'Predicted':>10s} {'Variance':>10s} {'%':>8s}")
    for c in sorted(comps, key=lambda x: x.predicted, reverse=True):
        flag = " ★" if c.source == closest.source else "  "
        print(f"{flag}{c.source:<34s} ${c.predicted:>9,.0f} "
              f"${c.variance:>+9,.0f} {c.variance_pct*100:>+7.1f}%")
    print()
    print(f"  CLOSEST: {closest.source}")
    print(f"  Variance: ${closest.variance:+.2f} ({closest.variance_pct*100:+.1f}%)")
    print()
    print("  ── Injection Ladder (pre-decided) ────────────────────────")
    print(f"  Tier matched:   {tier.label}")
    print(f"  Inject:         ${tier.inject_usd:,.0f}")
    print(f"  Phase action:   {tier.phase_action}")
    print()
    if per_market_rows:
        print("  ── Top 8 per-market variances (largest deltas first) ────")
        print(f"  {'Ticker':<38s} {'Pred':>8s} {'Actual':>8s} {'Var':>8s}")
        for r in per_market_rows[:8]:
            print(f"  {r['ticker'][:36]:<38s} "
                  f"${r['predicted']:>7.2f} "
                  f"${r['actual']:>7.2f} "
                  f"${r['variance']:>+7.2f}")
        print()
    print("══════════════════════════════════════════════════════════════")
    print()


# ── Entry ──────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--actual", type=float, required=True,
                   help="Actual weekly LIP payout from Kalshi (USD)")
    p.add_argument("--per-market", type=str, default=None,
                   help="CSV of ticker,actual_usd for per-market reconciliation")
    p.add_argument("--anchor", type=str, default=None,
                   help="ISO date in the week (default: current)")
    p.add_argument("--no-persist", action="store_true",
                   help="Skip writing to apr28_reconciliation_log table")
    a = p.parse_args()

    anchor = (datetime.fromisoformat(a.anchor).replace(tzinfo=timezone.utc)
              if a.anchor else None)
    pred = predict_week(week_start=anchor)

    if "error" in pred:
        print(f"  predictor error: {pred['error']}")
        return 1

    week_start = pred["week_start"]
    week_end = pred["week_end"]
    live_phantom_corrected = pred["total_weekly_predicted"]
    resting_pct = pred.get("total_resting_pct")

    comps = compare_predictions(
        actual=a.actual,
        baselines=FROZEN_BASELINES_2026_04_28,
        live_phantom_corrected=live_phantom_corrected,
    )
    closest = find_closest(comps)
    tier = find_tier(a.actual)

    per_market_rows = None
    if a.per_market:
        actuals = load_per_market_actuals(a.per_market)
        per_market_rows = per_market_reconciliation(
            actual_per_market=actuals,
            predicted_per_market=pred.get("per_market", []),
        )

    print_report(
        actual=a.actual,
        week_start=week_start,
        week_end=week_end,
        comps=comps,
        closest=closest,
        tier=tier,
        per_market_rows=per_market_rows,
        pred_total_resting_pct=resting_pct,
    )

    if not a.no_persist:
        persist_reconciliation(
            week_start=week_start, week_end=week_end,
            actual=a.actual, comps=comps, closest=closest, tier=tier,
        )
        print(f"  ✓ Saved to apr28_reconciliation_log (week_start={week_start[:10]})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
