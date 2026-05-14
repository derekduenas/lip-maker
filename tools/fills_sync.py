"""Fill sync — poll Kalshi /portfolio/fills and update local quotes table.

The WS fill handler was never wired, so the quotes table never sees status=filled.
This poller closes the gap. Runs every 60s via systemd timer.

Actions per fill:
  1. Match by order_id → update quotes row (status=filled, fill_size, fill_price, filled_at)
  2. Upsert into inventory table (net position per ticker+side)

Idempotent: safe to re-run; UPSERT by order_id + trade_id.
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


def ensure_fill_ledger(db_path: str = settings.DB_PATH):
    """Create fill_ledger to track which Kalshi fills we've processed."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fill_ledger (
            trade_id          TEXT PRIMARY KEY,
            order_id          TEXT NOT NULL,
            ticker            TEXT NOT NULL,
            side              TEXT NOT NULL,
            count             INTEGER NOT NULL,
            yes_price_cents   INTEGER,
            no_price_cents    INTEGER,
            is_taker          INTEGER,
            created_at        TEXT NOT NULL,
            synced_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fill_ledger_ticker ON fill_ledger(ticker);
        CREATE INDEX IF NOT EXISTS idx_fill_ledger_order  ON fill_ledger(order_id);
        """)
        conn.commit()
    finally:
        conn.close()


def sync_fills(db_path: str = settings.DB_PATH, lookback_limit: int = 200,
               max_pages: int = 10) -> dict:
    """Paginate through Kalshi fills (audit #7). Previously capped at 200."""
    ensure_fill_ledger(db_path)
    k = KalshiClient()
    fills = []
    cursor = None
    for _ in range(max_pages):
        params = {"limit": lookback_limit}
        if cursor: params["cursor"] = cursor
        try:
            resp = k.get("/portfolio/fills", params=params)
        except Exception as e:
            _log.error(f"fills fetch failed at cursor={cursor}: {e}")
            break
        fills.extend(resp.get("fills", []))
        cursor = resp.get("cursor")
        if not cursor: break

    conn = sqlite3.connect(db_path)
    try:
        new_ledger = 0
        quote_updates = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        for f in fills:
            trade_id = f.get("trade_id")
            order_id = f.get("order_id")
            if not trade_id or not order_id:
                continue

            # Skip if we already logged this fill
            already = conn.execute(
                "SELECT 1 FROM fill_ledger WHERE trade_id=?", (trade_id,)
            ).fetchone()
            if already:
                continue

            # Price in dollars string → cents int
            yp = float(f.get("yes_price_dollars", "0") or 0)
            np_ = float(f.get("no_price_dollars", "0") or 0)
            yp_c = int(round(yp * 100))
            np_c = int(round(np_ * 100))
            cnt_real = float(f.get("count_fp", "0") or 0)
            cnt = int(cnt_real)  # legacy column kept for back-compat
            side = f.get("side", "?")
            ticker = f.get("ticker", f.get("market_ticker", "?"))

            conn.execute(
                """INSERT INTO fill_ledger
                   (trade_id, order_id, ticker, side, count, count_real,
                    yes_price_cents, no_price_cents, is_taker,
                    created_at, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (trade_id, order_id, ticker, side, cnt, cnt_real,
                 yp_c, np_c, 1 if f.get("is_taker") else 0,
                 f.get("created_time", now_iso), now_iso),
            )
            new_ledger += 1

            # A.4 (2026-05-14): trigger markout computation for this fill.
            # Best-effort — if book history is too thin, backfill_pending
            # will pick it up on the next sweep. Wrapped in try so a markout
            # failure never blocks fill ledger persistence.
            try:
                from monitor.markout_logger import (
                    ensure_schema as _ml_ensure, compute_markouts_for_fill,
                )
                _ml_ensure(db_path)
                _fp = yp_c if (side or "").lower() == "yes" else np_c
                if _fp is not None:
                    _ts = datetime.fromisoformat(
                        f.get("created_time", now_iso).replace("Z", "+00:00")
                    ).timestamp()
                    compute_markouts_for_fill(
                        fill_id=trade_id, ticker=ticker, side=side or "",
                        fill_price_c=int(_fp), fill_size=int(cnt_real or cnt),
                        fill_ts=_ts, db_path=db_path,
                    )
            except Exception as _e:
                _log.debug(f"markout hook failed for {trade_id}: {_e}")

            # Update matching quote row (fill_price_cents = side's execution price)
            fill_price = yp_c if side == "yes" else np_c
            rc = conn.execute(
                """UPDATE quotes
                      SET status='filled',
                          fill_size = COALESCE(fill_size, 0) + ?,
                          fill_price_cents = ?,
                          filled_at = ?
                    WHERE order_id = ? AND status IN ('resting', 'pending')""",
                (cnt, fill_price, f.get("created_time", now_iso), order_id),
            ).rowcount
            quote_updates += rc

        conn.commit()

        # Recompute inventory — audit #9: gross_usd must be OPEN EXPOSURE
        # (|net_yes| × avg_price_per_contract), NOT cumulative traded notional.
        # Cumulative would inflate past the per-market safety cap even when
        # actual open position is zero.
        per_market = conn.execute(
            """SELECT ticker,
                      SUM(CASE WHEN side='yes' THEN count ELSE -count END) AS net_yes,
                      SUM(CASE WHEN side='yes' THEN count ELSE 0 END)   AS yes_cnt,
                      SUM(CASE WHEN side='yes' THEN count*yes_price_cents ELSE 0 END) AS yes_cost,
                      SUM(CASE WHEN side='no'  THEN count ELSE 0 END)   AS no_cnt,
                      SUM(CASE WHEN side='no'  THEN count*no_price_cents  ELSE 0 END) AS no_cost
               FROM fill_ledger
               GROUP BY ticker"""
        ).fetchall()

        for tkr, net_yes, yes_cnt, yes_cost, no_cnt, no_cost in per_market:
            avg_yes = (yes_cost / yes_cnt / 100.0) if yes_cnt else None
            avg_no  = (no_cost  / no_cnt  / 100.0) if no_cnt  else None
            # Open exposure: |net| × relevant avg price.
            # If net_yes > 0 → net long YES → exposure = net_yes × avg_yes_entry
            # If net_yes < 0 → net long NO  → exposure = |net_yes| × avg_no_entry
            net = int(net_yes or 0)
            if net > 0 and avg_yes is not None:
                gross_open = net * avg_yes
            elif net < 0 and avg_no is not None:
                gross_open = abs(net) * avg_no
            else:
                gross_open = 0.0
            conn.execute(
                """INSERT INTO inventory
                     (market_ticker, net_yes_contracts, gross_usd, avg_yes_entry, avg_no_entry, last_updated)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(market_ticker) DO UPDATE SET
                     net_yes_contracts = excluded.net_yes_contracts,
                     gross_usd         = excluded.gross_usd,
                     avg_yes_entry     = excluded.avg_yes_entry,
                     avg_no_entry      = excluded.avg_no_entry,
                     last_updated      = excluded.last_updated""",
                (tkr, net, round(gross_open, 4), avg_yes, avg_no, now_iso),
            )
        conn.commit()

        return {
            "kalshi_fills_returned": len(fills),
            "new_ledger_entries":    new_ledger,
            "quote_rows_updated":    quote_updates,
            "inventory_rows":        len(per_market),
        }
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=200)
    a = p.parse_args()

    s = sync_fills(lookback_limit=a.limit)
    print(f"kalshi_fills={s['kalshi_fills_returned']}  "
          f"new_ledger={s['new_ledger_entries']}  "
          f"quotes_updated={s['quote_rows_updated']}  "
          f"inventory_rows={s['inventory_rows']}")


if __name__ == "__main__":
    main()
