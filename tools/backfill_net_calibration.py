"""Net-capture calibration backfill — replaces rebate-only ratio with the
actual money-we-net ratio per series. Single transaction, no API calls.

Original metric: rebate_earned_usd / pool_per_day
New metric:      (rebate_earned_usd - max(0, -realized_pnl)) / pool_per_day

KXCOFFEEW example:
  Old: $24.76 rebate / $1,032 pool = 2.4% capture → looks viable
  New: ($24.76 - $71.61 loss) / $1,032 pool = -4.5% capture → blocked

When net capture is < 0 we floor at 0 (calibration is unsigned in downstream
math). When >0 we still cap at 5.0 (same as before) for numerical safety.
"""
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = "/root/lip-maker/data/lip_maker.db"
ALPHA = 0.05
RATIO_MAX = 5.0
RATIO_MIN_FLOOR = 0.0
MIN_PREDICTED_USD = 0.50
FALLBACK_CALIB = 0.25


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Pull each settled-with-rebate row paired with its pool size.
    # our_realized_usd captures position P&L (negative = loss = adverse selection).
    # rebate_earned_usd is the LIP payout for that ticker.
    rows = conn.execute("""
        SELECT s.series_prefix, s.rebate_earned_usd, s.our_realized_usd,
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

    per_series: dict[str, dict] = {}
    for prefix, rebate, realized, pool in rows:
        rebate = float(rebate or 0)
        realized = float(realized or 0)
        # Net = rebate minus adverse-selection loss (only count losses, not gains)
        adverse_cost = max(0.0, -realized)
        net = rebate - adverse_cost
        # Floor at 0 for downstream sizing math; cap at 5 for outliers
        ratio = max(RATIO_MIN_FLOOR, min(RATIO_MAX, net / pool))
        st = per_series.setdefault(prefix, {
            "calib": FALLBACK_CALIB,
            "n": 0,
            "last_ratio": None,
            "last_predicted": None,
            "last_actual": None,
            "raw_rebate_capture": FALLBACK_CALIB,
            "total_rebate": 0.0,
            "total_adverse": 0.0,
        })
        st["calib"] = (1 - ALPHA) * st["calib"] + ALPHA * ratio
        rebate_ratio = max(0.0, min(RATIO_MAX, rebate / pool))
        st["raw_rebate_capture"] = (1 - ALPHA) * st["raw_rebate_capture"] + ALPHA * rebate_ratio
        st["n"] += 1
        st["last_ratio"] = ratio
        st["last_predicted"] = pool
        st["last_actual"] = net
        st["total_rebate"] += rebate
        st["total_adverse"] += adverse_cost

    print(f"computed net calibrations for {len(per_series)} series")
    print()
    print(f"{'series':30s} {'net_calib':>10s} {'rebate_calib':>13s} {'n':>5s} {'rebate$':>9s} {'adverse$':>10s}")
    print("-" * 88)
    for prefix in sorted(per_series.keys(), key=lambda k: -per_series[k]["n"])[:30]:
        st = per_series[prefix]
        print(f"{prefix:30s} {st['calib']:>10.4f} {st['raw_rebate_capture']:>13.4f} "
              f"{st['n']:>5d} {st['total_rebate']:>9.2f} {st['total_adverse']:>10.2f}")

    now_iso = datetime.now(timezone.utc).isoformat()
    written = 0
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
            written += 1
    conn.close()
    print()
    print(f"wrote {written} rows to market_calibration (now net-capture, not rebate-only)")


if __name__ == "__main__":
    main()
