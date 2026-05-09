"""Auto-calibration loop — runs every hour to learn the projection-vs-reality
gap PER SERIES, and persists a calibration multiplier the allocator uses to
correct future picks.

Without this, capital_allocator uses hardcoded KALSHI_CALIB=0.25 forever.
The funnel showed PROJECTED $14.88/d vs EST_REBATE $7.65/d = 51% calibration —
half of what the model predicts. Different series have different calibration
ratios (some higher than 0.25, some way lower). Hardcoding one value across
all series wastes capital.

This script:
  1. For each series with quote activity in the last 1h, computes
     actual_share × pool / per_snap_estimate (from lip_snapshots) ÷
     projected (yield_equation prediction)
  2. EWMA blends: new_cal = 0.7 × current + 0.3 × measured
  3. Persists to series_calibration table
  4. Allocator reads this on next refresh

Cron every 60 minutes. Cold-start safe (no entries → use default 1.0 = no
adjustment until data accumulates).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

# EWMA blend factor. Higher = more weight on new sample. 0.3 = ~3-cycle half-life.
EWMA_ALPHA = 0.30
# Min snapshots in window before we trust a reading.
MIN_SNAPS_FOR_CAL = 10
# Cap calibration to avoid runaway. Real ratios should land in [0.1, 2.0].
CAL_MIN, CAL_MAX = 0.10, 2.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS series_calibration (
  series_ticker         TEXT PRIMARY KEY,
  calibration_ratio     REAL NOT NULL DEFAULT 1.0,
  samples               INTEGER NOT NULL DEFAULT 0,
  last_updated          TEXT NOT NULL,
  last_measured_ratio   REAL,
  last_projected_per_d  REAL,
  last_actual_per_d     REAL
);
CREATE INDEX IF NOT EXISTS idx_seriescal_updated ON series_calibration(last_updated);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def calibrate(db_path: str = settings.DB_PATH) -> dict:
    """Compute per-series calibration ratios and persist.

    NEW FORMULA (2026-05-03 — share-based, not rebate-based):
      actual_share   = avg(our_score / total_score)  [from lip_snapshots]
      expected_share = our_size / (our_size + assumed_competition)
        where assumed_competition = recent series_prior comp estimate

      calibration_ratio = actual_share / expected_share

    Interpretation:
      ratio < 1 → real competition higher than allocator assumed → demote
      ratio > 1 → real competition lower → boost
      ratio = 1 → calibration on-target

    This is meaningful TODAY using snapshot data, no need to wait for
    settlement payouts. Once settlement data exists, a future v2 can blend
    snapshot-share calibration with realized-rebate calibration.

    Returns dict of series → updated calibration ratio.
    """
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        # Per-series: actual share + ours_score + total_score from snapshots
        rows = conn.execute("""
            SELECT substr(s.market_ticker,1,instr(s.market_ticker||'-','-')-1) AS series,
                   COUNT(*) AS snaps,
                   SUM(s.snapshot_valid) AS valid,
                   AVG(CASE WHEN s.total_score > 0
                            THEN s.our_score * 1.0 / s.total_score ELSE 0 END) AS avg_share,
                   AVG(s.our_score) AS avg_our,
                   AVG(s.total_score) AS avg_total,
                   AVG(p.target_size) AS avg_target
            FROM lip_snapshots s
            JOIN lip_programs p ON s.market_ticker = p.market_ticker
            WHERE datetime(s.captured_at) > datetime('now','-1 hour')
              AND p.enrolled = 1
              AND s.snapshot_valid = 1
            GROUP BY series
            HAVING snaps >= ? AND avg_our > 0
        """, (MIN_SNAPS_FOR_CAL,)).fetchall()

        # Existing calibration values (ratio, samples) keyed by series
        existing = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT series_ticker, calibration_ratio, samples FROM series_calibration"
        ).fetchall()}

        updates = {}
        for r in rows:
            series, snaps, valid, actual_share, avg_our, avg_total, avg_target = r
            if not series or actual_share is None or actual_share <= 0:
                continue
            # Expected share: what the allocator's blind_prior would predict,
            # given the size we actually placed and the same blind comp estimate.
            # blind_prior comp = target × 0.4 (per capital_allocator).
            assumed_comp = max(1.0, (avg_target or 300) * 0.4)
            our_size_proxy = avg_our  # the size that produced these snapshots
            expected_share = our_size_proxy / (our_size_proxy + assumed_comp)
            if expected_share <= 0:
                continue

            measured_ratio = float(actual_share) / float(expected_share)
            # Cap to sensible range to avoid runaway from tiny denominators
            measured_ratio = max(CAL_MIN, min(CAL_MAX, measured_ratio))

            # EWMA blend with prior
            prior_cal, prior_n = existing.get(series, (1.0, 0))
            new_cal = (1 - EWMA_ALPHA) * prior_cal + EWMA_ALPHA * measured_ratio
            new_cal = max(CAL_MIN, min(CAL_MAX, new_cal))

            updates[series] = {
                "ratio":          round(new_cal, 4),
                "measured":       round(measured_ratio, 4),
                "actual_share":   round(actual_share, 4),
                "expected_share": round(expected_share, 4),
                "samples":        prior_n + snaps,
            }

        if updates:
            conn.executemany(
                """INSERT OR REPLACE INTO series_calibration
                   (series_ticker, calibration_ratio, samples, last_updated,
                    last_measured_ratio, last_projected_per_d, last_actual_per_d)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(s, d["ratio"], d["samples"], now_iso,
                  d["measured"], d["expected_share"], d["actual_share"])
                 for s, d in updates.items()],
            )
            conn.commit()

        _log.info(f"auto_calibrate: updated {len(updates)} series; "
                  f"sample_ratios={ {s: d['measured'] for s, d in list(updates.items())[:5]} }")
        return updates
    finally:
        conn.close()


def get_calibration(series: str, db_path: str = settings.DB_PATH) -> float:
    """Allocator helper — return per-series calibration multiplier or 1.0."""
    try:
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT calibration_ratio FROM series_calibration WHERE series_ticker = ?",
                (series,),
            ).fetchone()
            return float(row[0]) if row else 1.0
    except Exception:
        return 1.0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    updates = calibrate()
    print(f"\n=== AUTO-CALIBRATION RESULTS ({len(updates)} series updated) ===")
    print(f"{'SERIES':<28} {'NEW_CAL':>8} {'MEASURED':>9} {'ACT_SHARE':>10} {'EXP_SHARE':>10}")
    print("-" * 75)
    for s in sorted(updates.keys(), key=lambda k: -updates[k]["ratio"]):
        d = updates[s]
        flag = "🚀" if d["measured"] > 1.2 else ("⚠" if d["measured"] < 0.8 else "✓")
        print(f"{s[:26]:<28} {d['ratio']:>8.3f} {d['measured']:>9.3f} "
              f"{d['actual_share']:>10.4f} {d['expected_share']:>10.4f}  {flag}")
