"""Daily realized-P&L snapshot.

Queries Kalshi /portfolio/positions for the current total realized_pnl_dollars.
Writes one row to daily_pnl_log per UTC day (UPSERT on date).

Downstream: ramp_controller reads from this table instead of the unpopulated
quotes table. Without this, ramp_controller sees 0 P&L and cannot advance phase.

Run daily at 23:50 UTC via systemd timer.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_auth import KalshiClient

_log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_pnl_log (
    day                  TEXT PRIMARY KEY,        -- YYYY-MM-DD UTC
    realized_pnl_usd     REAL NOT NULL,           -- cumulative (not daily delta)
    daily_realized_delta REAL,                    -- today - yesterday
    open_exposure_usd    REAL,
    open_positions       INTEGER,
    total_fills_to_date  INTEGER,
    balance_usd          REAL,                    -- cash only (legacy)
    portfolio_value_usd  REAL,                    -- 2026-05-12: open-position MTM
    total_nav_usd        REAL,                    -- 2026-05-12: cash + portfolio = honest wealth
    snapshot_at          TEXT NOT NULL
);
"""

# Idempotent ALTER for existing DBs
ALTER_STMTS = [
    "ALTER TABLE daily_pnl_log ADD COLUMN portfolio_value_usd REAL",
    "ALTER TABLE daily_pnl_log ADD COLUMN total_nav_usd REAL",
]


def snapshot(db_path: str = settings.DB_PATH) -> dict:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for stmt in ALTER_STMTS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass    # column already exists
    conn.commit()

    try:
        k = KalshiClient()

        # Paginate positions (audit #7: prior code dropped fills beyond 200)
        mp = []
        cursor = None
        for _ in range(10):  # max 10 pages = 2000 positions
            params = {"limit": 200}
            if cursor: params["cursor"] = cursor
            try:
                pos = k.get("/portfolio/positions", params=params)
            except Exception as e:
                # Audit #6: Kalshi API failure — skip snapshot entirely, don't write bogus $0 row
                _log.error(f"Kalshi /portfolio/positions failed: {e}. SKIPPING daily snapshot.")
                return {"skipped": True, "reason": f"kalshi_api_failure: {e}"}
            mp.extend(pos.get("market_positions", []))
            cursor = pos.get("cursor")
            if not cursor: break

        open_realized = sum(float(x.get("realized_pnl_dollars", "0") or 0) for x in mp)
        exposure = sum(float(x.get("market_exposure_dollars", "0") or 0) for x in mp)
        open_n   = sum(1 for x in mp if float(x.get("position_fp", "0") or 0) != 0)

        # 2026-04-22 FIX: Kalshi's realized_pnl_dollars only captures realized on
        # CURRENTLY-OPEN positions. Once a market settles, positions drop out of
        # /portfolio/positions — their P&L flows to balance but we lose visibility.
        # Union: open-position realized + settlement_log filtered by close_time
        # up through the snapshot day (so each day gets its own settles).
        day_end_utc = datetime.now(timezone.utc).replace(
            hour=23, minute=59, second=59, microsecond=0).isoformat()
        try:
            settled_cum_row = conn.execute(
                """SELECT COALESCE(SUM(our_realized_usd), 0) FROM settlement_log
                   WHERE close_time <= ?""",
                (day_end_utc,),
            ).fetchone()
            settled_cum = float(settled_cum_row[0] or 0)
        except sqlite3.OperationalError:
            settled_cum = 0.0

        realized = open_realized + settled_cum

        # Fill count from ledger (authoritative)
        fills_n = conn.execute("SELECT COUNT(*) FROM fill_ledger").fetchone()[0]

        # Balance + portfolio_value (NAV-truth: cash + open-position MTM).
        # 2026-05-12: balance_usd alone misled the dashboard — cash drops as
        # engine deploys into positions are NOT losses; wealth shifts into
        # portfolio. Always pull both and compute total_nav.
        try:
            br = k.get("/portfolio/balance")
            balance         = float(br.get("balance", 0)) / 100.0
            portfolio_value = float(br.get("portfolio_value", 0)) / 100.0
            total_nav       = balance + portfolio_value
        except Exception as e:
            _log.error(f"Kalshi balance fetch failed: {e}. SKIPPING daily snapshot.")
            return {"skipped": True, "reason": f"balance_fetch_failed: {e}"}

        # Sanity: if realized == 0 AND we had prior realized > 0, something's wrong
        # (unlikely to genuinely revert to 0). Skip.
        prev_row = conn.execute(
            "SELECT realized_pnl_usd FROM daily_pnl_log WHERE day < ? ORDER BY day DESC LIMIT 1",
            (datetime.now(timezone.utc).date().isoformat(),),
        ).fetchone()
        if prev_row and prev_row[0] and abs(prev_row[0]) > 1.0 and realized == 0:
            _log.error(f"Suspicious: realized=$0 but prior was ${prev_row[0]:.2f}. SKIPPING.")
            return {"skipped": True, "reason": "suspicious_zero_realized"}

        now = datetime.now(timezone.utc)
        day = now.date().isoformat()

        # Delta vs yesterday (reuse prev_row from sanity check)
        prev_realized = prev_row[0] if prev_row and prev_row[0] is not None else 0
        daily_delta = realized - prev_realized

        conn.execute(
            """INSERT INTO daily_pnl_log
               (day, realized_pnl_usd, daily_realized_delta, open_exposure_usd,
                open_positions, total_fills_to_date, balance_usd,
                portfolio_value_usd, total_nav_usd, snapshot_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET
                 realized_pnl_usd     = excluded.realized_pnl_usd,
                 daily_realized_delta = excluded.daily_realized_delta,
                 open_exposure_usd    = excluded.open_exposure_usd,
                 open_positions       = excluded.open_positions,
                 total_fills_to_date  = excluded.total_fills_to_date,
                 balance_usd          = excluded.balance_usd,
                 portfolio_value_usd  = excluded.portfolio_value_usd,
                 total_nav_usd        = excluded.total_nav_usd,
                 snapshot_at          = excluded.snapshot_at""",
            (day, realized, daily_delta, exposure, open_n, fills_n,
             balance, portfolio_value, total_nav, now.isoformat()),
        )
        conn.commit()
        return {
            "day":             day,
            "realized":        round(realized, 2),
            "open_realized":   round(open_realized, 2),
            "settled_cum":     round(settled_cum, 2),
            "delta":           round(daily_delta, 2),
            "exposure":        round(exposure, 2),
            "positions":       open_n,
            "fills":           fills_n,
            "balance":         round(balance, 2),
            "portfolio_value": round(portfolio_value, 2),
            "total_nav":       round(total_nav, 2),
        }
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    a = p.parse_args()
    r = snapshot()
    if r.get("skipped"):
        print(f"daily_pnl snapshot SKIPPED: {r['reason']}")
        return
    print(f"daily_pnl snapshot:  day={r['day']}")
    print(f"  realized=${r['realized']:+.2f}  (open_pos=${r['open_realized']:+.2f} + settled=${r['settled_cum']:+.2f})")
    print(f"  delta=${r['delta']:+.2f}  exposure=${r['exposure']:.2f}  "
          f"positions={r['positions']}  fills={r['fills']}  balance=${r['balance']:.2f}")


if __name__ == "__main__":
    main()
