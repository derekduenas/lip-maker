"""Volume Incentive Program (VIP) tracker.

Per Kalshi docs (Aug 2025 CFTC filing + Help Center): VIP is a SEPARATE
rebate pool from LIP. Pays makers based on filled volume, capped at $0.005
per contract on trades at $0.03–$0.97.

VIP is structurally different from LIP:
  - LIP rewards RESTING orders (snapshot scoring)
  - VIP rewards FILLED orders (post-fill credit)

This means VIP partially offsets the adverse-selection cost of being filled —
a counter-rotation we should price into our profitability model.

This tracker:
  1. Discovers active VIP markets via /incentive_programs?type=volume
  2. Computes per-day accrual = min(filled_contracts × $0.005, market_pool_share)
  3. Persists to vip_accrual_log
  4. Sums per-day for daily TNP enrichment
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
from execution.kalshi_auth import KalshiClient

_log = logging.getLogger(__name__)

VIP_PER_CONTRACT_CAP_USD = 0.005   # per Kalshi VIP docs
VIP_PRICE_MIN = 0.03
VIP_PRICE_MAX = 0.97

SCHEMA = """
CREATE TABLE IF NOT EXISTS vip_programs (
    id                       TEXT PRIMARY KEY,
    market_ticker            TEXT NOT NULL,
    series_ticker            TEXT,
    start_date               TEXT NOT NULL,
    end_date                 TEXT NOT NULL,
    period_reward_usd        REAL NOT NULL,
    paid_out                 INTEGER NOT NULL DEFAULT 0,
    last_seen                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vip_market  ON vip_programs(market_ticker);
CREATE INDEX IF NOT EXISTS idx_vip_series  ON vip_programs(series_ticker);

CREATE TABLE IF NOT EXISTS vip_accrual_log (
    day                  TEXT PRIMARY KEY,
    eligible_fills       INTEGER NOT NULL,
    eligible_contracts   INTEGER NOT NULL,
    accrued_vip_usd      REAL NOT NULL,
    snapshot_at          TEXT NOT NULL
);
"""


def discover(*, status: str = "active", save: bool = True) -> list[dict]:
    """Mirror lip_discovery.discover() but for VIP type. Persists to vip_programs."""
    c = KalshiClient()
    programs: list[dict] = []
    cursor = None

    while True:
        params = {"status": status, "type": "volume", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = c.get_unauth("/incentive_programs", params=params)
        except Exception as e:
            _log.warning(f"/incentive_programs (volume) failed: {e}")
            break
        batch = resp.get("incentive_programs", [])
        for raw in batch:
            if raw.get("incentive_type") != "volume":
                continue
            # Parse defensively — schema may differ slightly from liquidity type
            try:
                programs.append({
                    "id":              raw.get("id", ""),
                    "market_ticker":   raw.get("market_ticker", ""),
                    "series_ticker":   raw.get("series_ticker"),
                    "start_date":      raw.get("start_date", ""),
                    "end_date":        raw.get("end_date", ""),
                    "period_reward_usd": float(raw.get("period_reward_value", 0)) / 100.0,
                    "paid_out":        1 if raw.get("paid_out") else 0,
                })
            except Exception as e:
                _log.warning(f"VIP parse failed: {e} on {raw}")
                continue
        cursor = resp.get("next_cursor")
        if not cursor:
            break

    if save and programs:
        conn = sqlite3.connect(settings.DB_PATH)
        conn.executescript(SCHEMA)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            for p in programs:
                conn.execute(
                    """INSERT OR REPLACE INTO vip_programs
                       (id, market_ticker, series_ticker, start_date, end_date,
                        period_reward_usd, paid_out, last_seen)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (p["id"], p["market_ticker"], p.get("series_ticker"),
                     p["start_date"], p["end_date"], p["period_reward_usd"],
                     p["paid_out"], now_iso),
                )
            conn.commit()
        finally:
            conn.close()

    _log.info(f"VIP discover: {len(programs)} programs")
    return programs


def compute_daily_accrual(day_iso: str, db_path: str = settings.DB_PATH) -> dict:
    """Sum eligible fills × $0.005 cap for a UTC day.

    Eligible fill = price between $0.03 and $0.97, executed on a VIP-enrolled
    market. We don't know the per-market pool share without Kalshi's data,
    so this is the UPPER BOUND. Actual VIP payout could be lower if our
    contracts × cap exceeds our pool share.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    day_start = day_iso + "T00:00:00+00:00"
    day_end   = day_iso + "T23:59:59+00:00"

    try:
        # All fills today
        rows = conn.execute(
            """SELECT f.ticker, f.side, f.count, f.yes_price_cents, f.no_price_cents
               FROM fill_ledger f
               WHERE datetime(f.created_at) >= datetime(?)
                 AND datetime(f.created_at) <= datetime(?)""",
            (day_start, day_end),
        ).fetchall()
    finally:
        # Don't close yet — need to do more queries
        pass

    eligible_fills = 0
    eligible_contracts = 0
    accrued = 0.0

    # Read VIP-enrolled tickers as a set for O(1) lookup
    vip_tickers = set()
    try:
        for (t,) in conn.execute(
            "SELECT market_ticker FROM vip_programs WHERE paid_out = 0"
        ).fetchall():
            vip_tickers.add(t)
    except sqlite3.OperationalError:
        pass

    for ticker, side, count, yp_c, np_c in rows:
        # Eligibility: fill price (the side actually executed) between 3¢ and 97¢
        fill_price_cents = yp_c if side == "yes" else np_c
        if fill_price_cents < 3 or fill_price_cents > 97:
            continue
        # Must be VIP-enrolled (if we have any VIP programs registered)
        # If vip_tickers is EMPTY, count all eligible-priced fills (assume everything)
        if vip_tickers and ticker not in vip_tickers:
            continue
        c = int(count)
        eligible_fills += 1
        eligible_contracts += c
        accrued += c * VIP_PER_CONTRACT_CAP_USD

    conn.execute(
        """INSERT OR REPLACE INTO vip_accrual_log
           (day, eligible_fills, eligible_contracts, accrued_vip_usd, snapshot_at)
           VALUES (?,?,?,?,?)""",
        (day_iso, eligible_fills, eligible_contracts, round(accrued, 4),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    return {
        "day":                day_iso,
        "eligible_fills":     eligible_fills,
        "eligible_contracts": eligible_contracts,
        "accrued_vip_usd":    round(accrued, 4),
        "vip_program_count":  len(vip_tickers),
        "scope":              "vip-enrolled-only" if vip_tickers else "all-eligible-priced",
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None, help="UTC day YYYY-MM-DD (default: today)")
    p.add_argument("--discover-only", action="store_true")
    a = p.parse_args()

    n = len(discover(save=True))
    print(f"VIP programs discovered + saved: {n}")

    if a.discover_only:
        return

    day = a.day or datetime.now(timezone.utc).date().isoformat()
    r = compute_daily_accrual(day)
    print(f"\nVIP accrual {day}:")
    print(f"  eligible fills:     {r['eligible_fills']}")
    print(f"  eligible contracts: {r['eligible_contracts']}")
    print(f"  accrued VIP USD:    ${r['accrued_vip_usd']}")
    print(f"  scope: {r['scope']} ({r['vip_program_count']} VIP markets registered)")


if __name__ == "__main__":
    main()
