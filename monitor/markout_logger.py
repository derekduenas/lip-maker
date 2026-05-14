"""Post-fill markout logger (Track A.4).

Records the microprice at fill-time and at t+1s/t+10s/t+60s after each fill,
then computes signed markout = sign(fill_side) × (microprice_at_t − fill_price).

Sign convention:
    positive signed_markout  →  microprice moved AGAINST our fill (we got
                                picked off; the trade was informed/toxic).
    negative signed_markout  →  microprice moved WITH our fill (we got
                                paid by uninformed flow).

PHYSICS
-------
A fill at price P on the YES side means we bought YES at P. If the YES
microprice 60s later is P+5, the market moved up after we bought → we
profited (negative markout convention). If it moved down to P−5, we got
picked off (positive markout).

For YES side (we are the buyer of YES):
    signed_markout = fill_price − mp_t          (positive → market dropped → toxic for us)
For NO side (we bought NO; equivalent to selling YES):
    signed_markout = mp_t − (100 − fill_price)  (positive → market moved up → toxic)

We store the simpler convention: positive = bad, negative = good, ALWAYS.

PIPELINE
--------
1. `run_paper.PaperRunner.heartbeat_snapshot_loop` calls
   `record_book_snapshot(ticker, mp, bid, ask, bid_sz, ask_sz, ts)` for each
   active market every 30s → `book_microprice_history` row.
2. `tools/fills_sync.py`, after inserting each new fill into `fill_ledger`,
   calls `compute_markouts_for_fill(fill_id, ticker, side, fill_price_cents,
   fill_ts)` → looks up the nearest-after history row at the three horizons,
   writes a `fill_markouts` row.
3. `tools/markout_report.py` aggregates daily and emits per-series median
   markout, the key Phase 3 readiness-gate signal.

USAGE
-----
    from monitor.markout_logger import ensure_schema, record_book_snapshot
    ensure_schema()
    record_book_snapshot("KXBRENTD-...", mp, bid, ask, bid_sz, ask_sz)
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)


# Tables we own.
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS book_microprice_history (
    ticker         TEXT    NOT NULL,
    captured_at    TEXT    NOT NULL,           -- ISO8601 UTC
    captured_ts    REAL    NOT NULL,           -- unix seconds
    microprice_c   REAL    NOT NULL,           -- cents [1, 99]
    best_bid_c     INTEGER,
    best_ask_c     INTEGER,
    bid_size       INTEGER,
    ask_size       INTEGER,
    PRIMARY KEY (ticker, captured_ts)
);

CREATE INDEX IF NOT EXISTS idx_book_microprice_ticker_ts
    ON book_microprice_history(ticker, captured_ts);

CREATE TABLE IF NOT EXISTS fill_markouts (
    fill_id          TEXT     PRIMARY KEY,     -- trade_id from fill_ledger
    ticker           TEXT     NOT NULL,
    side             TEXT     NOT NULL,        -- 'yes' / 'no'
    fill_price_c     INTEGER  NOT NULL,        -- our fill price (in cents)
    fill_size        INTEGER  NOT NULL,
    fill_ts          REAL     NOT NULL,
    -- microprice at fill time (nearest history row before fill_ts)
    mp_at_fill_c     REAL,
    -- microprice at fill_ts + horizon (nearest after)
    mp_t1s_c         REAL,
    mp_t10s_c        REAL,
    mp_t60s_c        REAL,
    -- signed markout in cents at each horizon (positive = toxic)
    markout_1s_c     REAL,
    markout_10s_c    REAL,
    markout_60s_c    REAL,
    computed_at      TEXT     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fill_markouts_ticker
    ON fill_markouts(ticker);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def record_book_snapshot(
    ticker: str,
    microprice_c: float,
    best_bid_c: Optional[int],
    best_ask_c: Optional[int],
    bid_size: Optional[int],
    ask_size: Optional[int],
    *,
    captured_ts: Optional[float] = None,
    db_path: str = settings.DB_PATH,
) -> None:
    """Persist a single (ticker, microprice, book) snapshot row.

    No-op if `microprice_c` is None. Idempotent on (ticker, captured_ts)
    primary key via INSERT OR REPLACE.
    """
    if microprice_c is None:
        return
    import time
    ts = captured_ts if captured_ts is not None else time.time()
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO book_microprice_history
                   (ticker, captured_at, captured_ts, microprice_c,
                    best_bid_c, best_ask_c, bid_size, ask_size)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (ticker, datetime.now(timezone.utc).isoformat(), ts,
                 float(microprice_c), best_bid_c, best_ask_c,
                 bid_size, ask_size),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log.debug(f"record_book_snapshot[{ticker}] failed: {e}")


def _nearest_microprice(
    conn: sqlite3.Connection,
    ticker: str,
    target_ts: float,
    after: bool,
    max_lag_sec: float = 120.0,
) -> Optional[float]:
    """Return microprice at the nearest history row before/after target_ts.

    Returns None if no row exists within `max_lag_sec` of target.
    """
    if after:
        row = conn.execute(
            """SELECT microprice_c, captured_ts FROM book_microprice_history
               WHERE ticker = ? AND captured_ts >= ? AND captured_ts <= ?
               ORDER BY captured_ts ASC LIMIT 1""",
            (ticker, target_ts, target_ts + max_lag_sec),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT microprice_c, captured_ts FROM book_microprice_history
               WHERE ticker = ? AND captured_ts <= ? AND captured_ts >= ?
               ORDER BY captured_ts DESC LIMIT 1""",
            (ticker, target_ts, target_ts - max_lag_sec),
        ).fetchone()
    return float(row[0]) if row else None


def _signed_markout(side: str, fill_price_c: float, mp_t_c: float) -> float:
    """Compute toxic-positive signed markout in cents.

    Args:
        side: 'yes' or 'no' — which side we filled on.
        fill_price_c: fill price in cents.
        mp_t_c: microprice (YES side) at the horizon.

    Returns:
        Positive when the fill was toxic for us; negative when benign.
    """
    if side.lower() == "yes":
        # We bought YES at fill_price_c. If YES microprice drops below
        # fill_price_c, we got picked off.
        return float(fill_price_c) - float(mp_t_c)
    # side == 'no': we bought NO at fill_price_c, equivalent to selling YES
    # at (100 - fill_price_c). If YES microprice rises above (100 - fill_price_c),
    # we got picked off.
    yes_equivalent = 100.0 - float(fill_price_c)
    return float(mp_t_c) - yes_equivalent


def compute_markouts_for_fill(
    fill_id: str,
    ticker: str,
    side: str,
    fill_price_c: int,
    fill_size: int,
    fill_ts: float,
    db_path: str = settings.DB_PATH,
) -> bool:
    """Compute and persist markouts at t+1s/+10s/+60s for one fill.

    Returns True if row written, False if not enough history available
    (caller should retry on the next markout_report sweep).
    """
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            # Skip if we've already processed this fill
            existing = conn.execute(
                "SELECT 1 FROM fill_markouts WHERE fill_id = ?",
                (fill_id,),
            ).fetchone()
            if existing:
                return True

            mp_fill = _nearest_microprice(conn, ticker, fill_ts, after=False)
            mp_1s   = _nearest_microprice(conn, ticker, fill_ts + 1.0,  after=True, max_lag_sec=15.0)
            mp_10s  = _nearest_microprice(conn, ticker, fill_ts + 10.0, after=True, max_lag_sec=30.0)
            mp_60s  = _nearest_microprice(conn, ticker, fill_ts + 60.0, after=True, max_lag_sec=90.0)

            # Need at minimum the t+60s point to be useful
            if mp_60s is None:
                return False
            # P1-3 fix (2026-05-14): also need a fair-value reference AT
            # fill time. Without mp_fill, downstream markout_report cannot
            # distinguish "we had no reference" from "fill was benign" —
            # the Phase 3 readiness gate would be biased toward false-positive
            # "go-live" verdicts. Fail closed; backfill_pending will retry
            # on the next sweep when book history is denser.
            if mp_fill is None:
                return False

            mo_1s  = _signed_markout(side, fill_price_c, mp_1s)  if mp_1s  is not None else None
            mo_10s = _signed_markout(side, fill_price_c, mp_10s) if mp_10s is not None else None
            mo_60s = _signed_markout(side, fill_price_c, mp_60s)

            conn.execute(
                """INSERT OR REPLACE INTO fill_markouts
                   (fill_id, ticker, side, fill_price_c, fill_size, fill_ts,
                    mp_at_fill_c, mp_t1s_c, mp_t10s_c, mp_t60s_c,
                    markout_1s_c, markout_10s_c, markout_60s_c, computed_at)
                   VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?)""",
                (fill_id, ticker, side.lower(), int(fill_price_c),
                 int(fill_size), float(fill_ts),
                 mp_fill, mp_1s, mp_10s, mp_60s,
                 mo_1s, mo_10s, mo_60s,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        _log.warning(f"compute_markouts_for_fill[{ticker}/{fill_id}] failed: {e}")
        return False


def backfill_pending(db_path: str = settings.DB_PATH,
                     lookback_sec: int = 3600) -> dict:
    """Sweep fill_ledger for recent fills not yet in fill_markouts.

    Designed to be run periodically (e.g. every 5 min via timer). Picks
    up fills where the book history is now mature enough to compute t+60s
    markouts.

    Returns {'processed': N, 'skipped_no_history': K}.
    """
    import time as _time
    ensure_schema(db_path)
    cutoff_ts = _time.time() - lookback_sec
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
    processed = 0
    skipped = 0
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        # fill_ledger uses created_at (ISO8601) — convert to unix for join
        rows = conn.execute(
            """SELECT f.trade_id, f.ticker, f.side, f.yes_price_cents,
                      f.no_price_cents, f.count, f.created_at
               FROM fill_ledger f
               LEFT JOIN fill_markouts m ON f.trade_id = m.fill_id
               WHERE m.fill_id IS NULL AND f.created_at >= ?
               ORDER BY f.created_at""",
            (cutoff_iso,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        # fill_ledger may not exist in a fresh paper-only db
        _log.info(f"backfill_pending: skipping ({e})")
        conn.close()
        return {"processed": 0, "skipped_no_history": 0,
                "skipped_no_table": True}
    finally:
        pass

    for trade_id, ticker, side, yp_c, np_c, count, created_at in rows:
        # Pick the side's fill price
        fill_price_c = yp_c if (side or "").lower() == "yes" else np_c
        if fill_price_c is None:
            continue
        try:
            ts = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            continue
        ok = compute_markouts_for_fill(
            fill_id=trade_id, ticker=ticker, side=side or "",
            fill_price_c=int(fill_price_c), fill_size=int(count or 0),
            fill_ts=ts, db_path=db_path,
        )
        if ok:
            processed += 1
        else:
            skipped += 1
    conn.close()
    return {"processed": processed, "skipped_no_history": skipped}


def _self_test() -> None:
    """Round-trip test using an in-memory sqlite db."""
    import time as _time, tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        ensure_schema(path)
        # Record snapshots simulating a market that moved from 50c → 53c over 60s
        t0 = _time.time()
        record_book_snapshot("TEST", 50.0, 49, 51, 100, 100,
                             captured_ts=t0 - 1, db_path=path)
        record_book_snapshot("TEST", 50.5, 50, 51, 100, 100,
                             captured_ts=t0 + 1, db_path=path)
        record_book_snapshot("TEST", 51.5, 51, 52, 100, 100,
                             captured_ts=t0 + 10, db_path=path)
        record_book_snapshot("TEST", 53.0, 52, 54, 100, 100,
                             captured_ts=t0 + 60, db_path=path)
        # Fill yes at 50 → benign (market moved up after we bought)
        ok = compute_markouts_for_fill("F1", "TEST", "yes", 50, 100,
                                       t0, db_path=path)
        assert ok, "should compute"
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT markout_60s_c FROM fill_markouts WHERE fill_id='F1'"
        ).fetchone()
        conn.close()
        mo60 = row[0]
        # fill=50, mp_t60=53 → benign: 50 - 53 = -3 (negative = good)
        assert abs(mo60 - (-3.0)) < 0.01, f"yes-fill markout expected -3, got {mo60}"

        # Fill no at 50 → toxic (market moved against no-buyer)
        ok = compute_markouts_for_fill("F2", "TEST", "no", 50, 100,
                                       t0, db_path=path)
        assert ok
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT markout_60s_c FROM fill_markouts WHERE fill_id='F2'"
        ).fetchone()
        conn.close()
        mo60 = row[0]
        # NO fill at 50 = equivalent to selling YES at (100-50)=50.
        # YES rose to 53 → we lost: 53 - 50 = +3 (positive = toxic)
        assert abs(mo60 - 3.0) < 0.01, f"no-fill markout expected +3, got {mo60}"

        # Idempotent
        ok = compute_markouts_for_fill("F1", "TEST", "yes", 50, 100,
                                       t0, db_path=path)
        assert ok  # already exists, returns True

        print("markout_logger self-test: PASS")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
