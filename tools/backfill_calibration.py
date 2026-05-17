"""Direct calibration backfill — bypass lock contention by reading all
needed data into memory, computing EWMA per series, and writing the
result in a single transaction with long timeout.

Inputs from DB:
  settlement_log.rebate_earned_usd → actual
  lip_programs.reward_per_day_usd  → predicted

Output:
  market_calibration rows (one per series_prefix)
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = "/root/lip-maker/data/lip_maker.db"
ALPHA = 0.05
RATIO_MIN = 0.0
RATIO_MAX = 5.0
MIN_PREDICTED_USD = 0.50
FALLBACK_CALIB = 0.25


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Pull all settlement+pool pairs in one query — no API calls needed
    rows = conn.execute("""
        SELECT s.series_prefix, s.ticker, s.rebate_earned_usd,
               p.reward_per_day_usd
        FROM settlement_log s
        LEFT JOIN lip_programs p ON p.market_ticker = s.ticker
        WHERE s.rebate_earned_usd IS NOT NULL
          AND s.rebate_earned_usd >= 0
          AND p.reward_per_day_usd IS NOT NULL
          AND p.reward_per_day_usd >= ?
        ORDER BY s.recorded_at ASC
    """, (MIN_PREDICTED_USD,)).fetchall()

    print(f"loaded {len(rows)} settlement+pool pairs")

    # Group by series, replay EWMA per series
    per_series: dict[str, dict] = {}
    for prefix, ticker, rebate, pool in rows:
        ratio = max(RATIO_MIN, min(RATIO_MAX, rebate / pool))
        st = per_series.setdefault(prefix, {
            "calib": FALLBACK_CALIB,
            "n": 0,
            "last_ratio": None,
            "last_predicted": None,
            "last_actual": None,
        })
        # EWMA update: new = (1-α)·old + α·obs
        st["calib"] = (1 - ALPHA) * st["calib"] + ALPHA * ratio
        st["n"] += 1
        st["last_ratio"] = ratio
        st["last_predicted"] = pool
        st["last_actual"] = rebate

    print(f"computed calibrations for {len(per_series)} series")
    print()
    print(f"{'series':30s} {'calib':>8s} {'n':>5s} {'last_ratio':>12s} {'capture_%':>10s}")
    print("-" * 75)
    for prefix in sorted(per_series.keys(), key=lambda k: -per_series[k]["n"])[:25]:
        st = per_series[prefix]
        print(f"{prefix:30s} {st['calib']:>8.4f} {st['n']:>5d} {st['last_ratio'] or 0:>12.4f} {st['calib']*100:>9.1f}%")

    # Single-transaction write — minimal lock window
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        for prefix, st in per_series.items():
            conn.execute("""
                INSERT INTO market_calibration
                    (key, calibration, n_samples, last_ratio,
                     last_predicted_usd, last_actual_usd, updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    calibration = excluded.calibration,
                    n_samples = excluded.n_samples,
                    last_ratio = excluded.last_ratio,
                    last_predicted_usd = excluded.last_predicted_usd,
                    last_actual_usd = excluded.last_actual_usd,
                    updated_at = excluded.updated_at
            """, (prefix, st["calib"], st["n"], st["last_ratio"],
                  st["last_predicted"], st["last_actual"], now_iso))
    conn.close()
    print()
    print(f"wrote {len(per_series)} rows to market_calibration")


if __name__ == "__main__":
    main()
