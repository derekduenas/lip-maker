"""Hedge unwind (B.6) — closes the hedge leg when the Kalshi leg disappears.

Without this, a dangling sequence can occur:
  T0: Kalshi long YES on KXBTCMAXMON-T70000, hedger sells 1 BTC on Kraken
  T1: Kalshi market settles → our YES position is reaped
  T2: Kraken short BTC remains open → we're now net short BTC unhedged

This module is invoked at settlement time (from settlement_reconciler) and
also exposed as a CLI for manual reconciliation. It finds open hedge_log
rows for a settled ticker and places the opposite trade via the same
broker adapter that opened them. Adapter handles dry_run vs live exactly
as the open-side path does — so paper-mode unwinds also log to
broker_dry_run_log (the synthetic pair) and live-mode unwinds place real
opposite orders.

SCHEMA MIGRATION
----------------
Adds `unwound_at TEXT` and `unwind_fill_id TEXT` columns to hedge_log if
not already present. Idempotent.

USAGE
-----
    from cross_venue.hedge_unwind import unwind_for_ticker, unwind_all_settled

    # Per-ticker (called from settlement_reconciler)
    unwind_for_ticker("KXBTCMAXMON-26MAY-T70000", db_path)

    # Bulk cleanup CLI
    python -m cross_venue.hedge_unwind --since 24h
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)


def ensure_unwind_schema(db_path: str = settings.DB_PATH) -> None:
    """Idempotent migration — add unwound_at and unwind_fill_id columns."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(hedge_log)")}
        if "unwound_at" not in existing:
            conn.execute("ALTER TABLE hedge_log ADD COLUMN unwound_at TEXT")
        if "unwind_fill_id" not in existing:
            conn.execute("ALTER TABLE hedge_log ADD COLUMN unwind_fill_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hedge_log_open "
            "ON hedge_log(kalshi_ticker, unwound_at)"
        )
        conn.commit()
    finally:
        conn.close()


def _open_hedges_for_ticker(ticker: str, db_path: str) -> list[dict]:
    """Return hedge_log rows that are 'placed' and not yet unwound."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, fill_id, kalshi_ticker, kalshi_side, kalshi_contracts,
                      hedge_instrument, hedge_venue, hedge_qty_int, status
               FROM hedge_log
               WHERE kalshi_ticker = ?
                 AND status = 'placed'
                 AND (unwound_at IS NULL OR unwound_at = '')
               ORDER BY id ASC""",
            (ticker,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _mark_unwound(hedge_log_id: int, unwind_fill_id: str,
                  db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute(
            "UPDATE hedge_log SET unwound_at = ?, unwind_fill_id = ? "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), unwind_fill_id,
             hedge_log_id),
        )
        conn.commit()
    finally:
        conn.close()


def unwind_for_ticker(ticker: str,
                      db_path: str = settings.DB_PATH) -> dict:
    """Unwind any open hedge positions for the given Kalshi ticker.

    Computes the opposite-side trade and routes through the same venue
    adapter used to open the position. Marks each hedge_log row as
    unwound and stores the closing fill_id for reconciliation.

    Returns {ticker, n_unwound, n_skipped, details: [{...}]}.
    """
    ensure_unwind_schema(db_path)
    out = {"ticker": ticker, "n_unwound": 0, "n_skipped": 0, "details": []}

    opens = _open_hedges_for_ticker(ticker, db_path)
    if not opens:
        return out

    for row in opens:
        qty_int = int(row["hedge_qty_int"] or 0)
        if qty_int == 0:
            out["n_skipped"] += 1
            out["details"].append({
                "hedge_log_id": row["id"], "reason": "qty_was_zero",
            })
            _mark_unwound(row["id"], "NA-zero-qty", db_path)
            continue

        # Opposite side
        # qty_int >0 means we BOUGHT (open) → to close, SELL same abs qty
        # qty_int <0 means we SOLD (open) → to close, BUY same abs qty
        close_side = "sell" if qty_int > 0 else "buy"
        abs_qty = abs(qty_int)
        venue = row["hedge_venue"]
        instrument = row["hedge_instrument"]

        try:
            if venue == "Kraken":
                from execution.kraken_adapter import KrakenAdapter
                adapter = KrakenAdapter(db_path=db_path)
            elif venue == "CME":
                from execution.ibkr_adapter import IBKRAdapter
                adapter = IBKRAdapter(db_path=db_path)
            elif venue in ("ICE", "Hyperliquid", "manual"):
                out["n_skipped"] += 1
                out["details"].append({
                    "hedge_log_id": row["id"],
                    "reason": f"manual_venue:{venue}",
                })
                _mark_unwound(row["id"], f"MANUAL-{venue}", db_path)
                continue
            else:
                out["n_skipped"] += 1
                out["details"].append({
                    "hedge_log_id": row["id"],
                    "reason": f"unknown_venue:{venue}",
                })
                continue

            result = adapter.place_market(
                instrument=instrument, qty=abs_qty, side=close_side,
                notes=f"unwind kalshi={ticker} orig_fill_id={row['fill_id']}",
            )
            if result.status in ("dry_run", "filled", "partial"):
                _mark_unwound(row["id"], result.fill_id, db_path)
                out["n_unwound"] += 1
                out["details"].append({
                    "hedge_log_id": row["id"], "venue": venue,
                    "instrument": instrument, "qty": abs_qty,
                    "side": close_side, "close_fill_id": result.fill_id,
                    "status": result.status,
                })
                _log.info(
                    f"[B.6 unwind] {venue}/{instrument} {close_side} {abs_qty} "
                    f"(closes hedge_log #{row['id']}, kalshi={ticker})"
                )
            else:
                out["n_skipped"] += 1
                out["details"].append({
                    "hedge_log_id": row["id"],
                    "reason": f"adapter_rejected:{result.detail or result.status}",
                })
                _log.warning(
                    f"[B.6 unwind] FAILED {venue}/{instrument} "
                    f"hedge_log #{row['id']}: {result.detail}"
                )
        except Exception as e:
            out["n_skipped"] += 1
            out["details"].append({
                "hedge_log_id": row["id"], "reason": f"exception:{e}",
            })
            _log.warning(f"[B.6 unwind] exception for #{row['id']}: {e}")

    return out


def unwind_all_settled(since_hours: int = 24,
                       db_path: str = settings.DB_PATH) -> dict:
    """Bulk unwind for all tickers that recently settled.

    Reads settlement_log for tickers settled in the window and processes
    each one. Idempotent — re-runs only touch hedges not yet unwound.
    """
    ensure_unwind_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cutoff = f"-{int(since_hours)} hours"
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM settlement_log "
            "WHERE datetime(recorded_at) > datetime('now', ?) "
            "ORDER BY recorded_at DESC",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    total = {"n_tickers": len(rows), "n_unwound": 0, "n_skipped": 0,
             "per_ticker": []}
    for (ticker,) in rows:
        res = unwind_for_ticker(ticker, db_path)
        total["n_unwound"] += res["n_unwound"]
        total["n_skipped"] += res["n_skipped"]
        if res["n_unwound"] > 0 or res["n_skipped"] > 0:
            total["per_ticker"].append(res)
    return total


def _self_test() -> None:
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Seed minimal schema + hedge_log row
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE hedge_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fill_id TEXT, kalshi_ticker TEXT, kalshi_side TEXT,
                kalshi_contracts INTEGER, kalshi_fill_price_c INTEGER,
                kalshi_strike REAL, hedge_instrument TEXT, hedge_venue TEXT,
                spot REAL, sigma_annual REAL, hours_to_settle REAL,
                hedge_qty_float REAL, hedge_qty_int INTEGER,
                hedge_qty_residual REAL, status TEXT, status_detail TEXT,
                created_at TEXT
            );
            CREATE TABLE broker_dry_run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT, instrument TEXT, qty INTEGER, side TEXT,
                intended_at TEXT, synthetic_fill_id TEXT, notes TEXT
            );
            CREATE TABLE broker_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT, status TEXT, detail TEXT, ts REAL, ts_iso TEXT
            );
            CREATE TABLE settlement_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT,
                recorded_at TEXT
            );
        """)
        conn.execute(
            "INSERT INTO hedge_log (fill_id, kalshi_ticker, kalshi_side, "
            "kalshi_contracts, hedge_instrument, hedge_venue, hedge_qty_int, "
            "status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("synth-fill-A", "KXBTCMAXMON-26MAY-T70000", "yes", 100,
             "XBTUSD", "Kraken", -3, "placed",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO settlement_log (ticker, recorded_at) VALUES (?,?)",
            ("KXBTCMAXMON-26MAY-T70000",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        # Unwind
        res = unwind_for_ticker("KXBTCMAXMON-26MAY-T70000", db_path=path)
        assert res["n_unwound"] == 1, res
        assert res["n_skipped"] == 0, res
        detail = res["details"][0]
        assert detail["side"] == "buy", detail
        assert detail["qty"] == 3, detail

        # Verify hedge_log marked
        conn = sqlite3.connect(path)
        r = conn.execute(
            "SELECT unwound_at, unwind_fill_id FROM hedge_log WHERE id=1"
        ).fetchone()
        assert r[0] is not None
        assert r[1].startswith("DRY-")
        # Verify broker_dry_run_log has the closing trade
        rr = conn.execute(
            "SELECT venue, instrument, qty, side FROM broker_dry_run_log "
            "WHERE notes LIKE '%unwind%'"
        ).fetchone()
        assert rr == ("kraken", "XBTUSD", 3, "buy"), rr

        # Re-run should be no-op (idempotent)
        res2 = unwind_for_ticker("KXBTCMAXMON-26MAY-T70000", db_path=path)
        assert res2["n_unwound"] == 0, res2
        conn.close()
        print("hedge_unwind self-test: PASS")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", help="single Kalshi ticker to unwind")
    p.add_argument("--since-hours", type=int, default=24,
                   help="bulk-unwind every ticker settled in the last N hours")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()

    if a.self_test:
        _self_test()
        return 0
    if a.ticker:
        out = unwind_for_ticker(a.ticker)
        print(f"unwound={out['n_unwound']} skipped={out['n_skipped']}")
        return 0
    out = unwind_all_settled(since_hours=a.since_hours)
    print(
        f"tickers_scanned={out['n_tickers']} "
        f"unwound={out['n_unwound']} skipped={out['n_skipped']}"
    )
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        sys.exit(_main())
