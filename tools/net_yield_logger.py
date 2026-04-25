"""Net Yield Logger — end-of-day TOTAL NET PROFIT snapshot.

The Golden Rule: TNP > 0. Total Net Profit = Rebate + Realized P&L - Fees.

Until now we've tracked REBATE (earned). This logs what we actually KEEP
after settlement outcomes. If net yield trends negative, the system is
bleeding and we alert.

Run daily at 23:55 UTC (after daily_pnl_snapshot which runs 23:50).
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS net_yield_log (
    day                   TEXT PRIMARY KEY,
    realized_delta_usd    REAL NOT NULL,    -- from daily_pnl_log
    est_rebate_accrual    REAL NOT NULL,    -- from lip_snapshots × reward per day
    fees_paid_usd         REAL DEFAULT 0,   -- not tracked yet; LIP is fee-free for makers
    total_net_profit_usd  REAL NOT NULL,    -- realized + rebate - fees
    positions_open        INTEGER,
    exposure_usd          REAL,
    balance_usd           REAL,
    trailing_7d_tnp       REAL,             -- sum of last 7 TNP values
    ratio_rebate_to_loss  REAL,             -- rebate / |realized_loss| ; NULL if no loss
    snapshot_at           TEXT NOT NULL
);
"""


def compute_rebate_accrual_for_day(day_iso: str, db_path: str) -> float:
    """Estimate rebate earned during a UTC day from snapshot data.

    Per CFTC-verified formula: share_i × reward_per_day_usd_i.
    Sum over markets weighted by fraction_of_day_scored.
    """
    conn = sqlite3.connect(db_path)
    try:
        # For each market active that day, compute avg share and hours_valid.
        day_start = day_iso + "T00:00:00+00:00"
        day_end   = day_iso + "T23:59:59+00:00"
        rows = conn.execute(
            """SELECT s.market_ticker,
                      COUNT(*) AS n_snaps,
                      AVG(CASE WHEN s.snapshot_valid=1 THEN s.our_score END)/2.0 AS share,
                      AVG(CASE WHEN s.snapshot_valid=1 THEN 1.0 ELSE 0 END) AS valid_pct,
                      p.reward_per_day_usd
               FROM lip_snapshots s
               LEFT JOIN lip_programs p ON s.market_ticker = p.market_ticker
               WHERE datetime(s.captured_at) >= datetime(?)
                 AND datetime(s.captured_at) <  datetime(?)
                 AND p.enrolled = 1
               GROUP BY s.market_ticker
               HAVING n_snaps >= 30""",
            (day_start, day_end),
        ).fetchall()
    finally:
        conn.close()

    total = 0.0
    for tkr, n, share, valid, reward in rows:
        if not reward: continue
        share = share or 0
        valid = valid or 0
        # Fraction-of-day adjustment: if we scored the market for 12h of 24h, pay 50%.
        # Snapshots are throttled to 1/5s + heartbeat 30s. 24h = 17,280 fast or 2880 hb.
        # Use 2880 as denom for "full day coverage" floor.
        day_coverage = min(1.0, n / 2880)
        total += share * valid * reward * day_coverage
    return round(total, 2)


def snapshot(day: str | None = None, db_path: str = settings.DB_PATH) -> dict:
    day = day or datetime.now(timezone.utc).date().isoformat()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()

    # Realized from daily_pnl_log (populated by daily_pnl_snapshot.py)
    row = conn.execute(
        """SELECT daily_realized_delta, open_positions, open_exposure_usd, balance_usd
           FROM daily_pnl_log WHERE day = ?""",
        (day,),
    ).fetchone()
    if not row:
        conn.close()
        return {"error": f"no daily_pnl_log row for {day} — run daily_pnl_snapshot first"}

    realized = row[0] or 0
    open_n   = row[1] or 0
    exposure = row[2] or 0
    balance  = row[3] or 0

    # Rebate accrual for the day
    rebate = compute_rebate_accrual_for_day(day, db_path)

    fees = 0.0  # LIP maker orders are fee-free per Kalshi
    tnp = realized + rebate - fees

    # 7-day trailing TNP (excluding today, to avoid chasing own tail)
    prior = conn.execute(
        """SELECT COALESCE(SUM(total_net_profit_usd), 0)
           FROM net_yield_log
           WHERE day >= date(?, '-7 day') AND day < ?""",
        (day, day),
    ).fetchone()[0]

    # rebate/loss ratio: if realized is negative, how many rebate-dollars per loss-dollar?
    ratio = None
    if realized < 0:
        ratio = round(rebate / abs(realized), 2) if abs(realized) > 0.01 else None

    conn.execute(
        """INSERT INTO net_yield_log
           (day, realized_delta_usd, est_rebate_accrual, fees_paid_usd,
            total_net_profit_usd, positions_open, exposure_usd, balance_usd,
            trailing_7d_tnp, ratio_rebate_to_loss, snapshot_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(day) DO UPDATE SET
             realized_delta_usd   = excluded.realized_delta_usd,
             est_rebate_accrual   = excluded.est_rebate_accrual,
             total_net_profit_usd = excluded.total_net_profit_usd,
             positions_open       = excluded.positions_open,
             exposure_usd         = excluded.exposure_usd,
             balance_usd          = excluded.balance_usd,
             trailing_7d_tnp      = excluded.trailing_7d_tnp,
             ratio_rebate_to_loss = excluded.ratio_rebate_to_loss,
             snapshot_at          = excluded.snapshot_at""",
        (day, round(realized, 4), rebate, fees, round(tnp, 4),
         open_n, round(exposure, 2), round(balance, 2),
         round(prior, 2), ratio,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    return {
        "day":       day,
        "realized":  round(realized, 2),
        "rebate":    rebate,
        "fees":      fees,
        "tnp":       round(tnp, 2),
        "trailing_7d": round(prior, 2),
        "ratio":     ratio,
        "balance":   round(balance, 2),
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None, help="UTC day YYYY-MM-DD (default: today)")
    a = p.parse_args()

    r = snapshot(day=a.day)
    if "error" in r:
        print(f"SKIPPED: {r['error']}")
        return
    print(f"\n══ NET YIELD — {r['day']} ══")
    print(f"  realized P&L:       ${r['realized']:+.2f}")
    print(f"  rebate accrual:     ${r['rebate']:+.2f}")
    print(f"  fees:               ${r['fees']:+.2f}")
    print(f"  ──────────────")
    print(f"  TOTAL NET PROFIT:   ${r['tnp']:+.2f}")
    print(f"  trailing 7d TNP:    ${r['trailing_7d']:+.2f}")
    if r['ratio'] is not None:
        print(f"  rebate/|loss| ratio: {r['ratio']}x  (target ≥ 3.0)")
    print(f"  balance:            ${r['balance']:.2f}")
    if r['tnp'] < 0:
        print(f"  🔴 ALERT: negative TNP — system bleeding")
    elif r['ratio'] is not None and r['ratio'] < 1.5:
        print(f"  🟡 WARN: ratio < 1.5x — rebate barely covering losses")
    else:
        print(f"  ✅ healthy")


if __name__ == "__main__":
    main()
