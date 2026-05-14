"""Graduated market throttle (Track A.3).

Replaces the existing binary blacklist with a (size_scale, tick_offset)
tuple per market. quote_manager.QuoteManager._passes_safety multiplies
target.size_contracts by size_scale and adds tick_offset (cents) to
yes/no bids.

Tier ladder (currently for VPIN; toxicity_filter and order_flow_tracker
will follow the same shape):

    VPIN < 0.50      → no row    (no throttle, full quote)
    0.50 ≤ VPIN < 0.60 → (size_scale=0.70, tick_off=0)
    0.60 ≤ VPIN < 0.70 → (size_scale=0.40, tick_off=1)
    0.70 ≤ VPIN < 0.80 → (size_scale=0.15, tick_off=2)
    VPIN ≥ 0.80      → (size_scale=0.00, tick_off=0)  ≡ binary ban

Schema is multi-source: rows tagged by `source` (vpin | toxicity |
order_flow | manual). When several sources have active rows for a
ticker, quote_manager takes the MIN size_scale and MAX tick_offset
(most conservative).

USAGE
-----
    from monitor.market_throttle import ensure_schema, upsert, active_for

    ensure_schema()
    upsert("KXBRENTD-26JUN0117-T100", size_scale=0.4, tick_offset=1,
           source="vpin", ttl_sec=600, detail="vpin=0.65")
    s, t = active_for("KXBRENTD-26JUN0117-T100")  # (0.4, 1)
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS market_throttle (
    ticker          TEXT NOT NULL,
    source          TEXT NOT NULL,           -- vpin | toxicity | order_flow | manual
    size_scale      REAL NOT NULL,           -- 0.0 .. 1.0
    tick_offset     INTEGER NOT NULL,        -- cents back from best (0..5)
    detail          TEXT,
    added_at        TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    PRIMARY KEY (ticker, source)
);

CREATE INDEX IF NOT EXISTS idx_market_throttle_ticker_exp
    ON market_throttle(ticker, expires_at);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


# ── Standard VPIN tier mapper ───────────────────────────────────────────────
# Used by vpin_gate; toxicity_filter / order_flow_tracker can define their own.

def vpin_tier(vpin: float) -> Optional[tuple[float, int, str]]:
    """Map a VPIN value to (size_scale, tick_offset, detail).

    Returns None if VPIN < 0.50 (no throttle row needed).
    """
    if vpin is None or vpin < 0.50:
        return None
    if vpin < 0.60:
        return (0.70, 0, f"vpin={vpin:.3f} tier-1")
    if vpin < 0.70:
        return (0.40, 1, f"vpin={vpin:.3f} tier-2")
    if vpin < 0.80:
        return (0.15, 2, f"vpin={vpin:.3f} tier-3")
    return (0.0, 0, f"vpin={vpin:.3f} tier-4 (full block)")


def upsert(
    ticker: str, *, size_scale: float, tick_offset: int,
    source: str, ttl_sec: int, detail: str = "",
    db_path: str = settings.DB_PATH,
) -> None:
    """Insert or replace a throttle row (per source). Idempotent.

    `ttl_sec` defines how long the row stays active; expires_at is
    computed at write time.
    """
    ensure_schema(db_path)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=int(ttl_sec))
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            conn.execute(
                """INSERT INTO market_throttle
                   (ticker, source, size_scale, tick_offset, detail,
                    added_at, expires_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(ticker, source) DO UPDATE SET
                       size_scale=excluded.size_scale,
                       tick_offset=excluded.tick_offset,
                       detail=excluded.detail,
                       added_at=excluded.added_at,
                       expires_at=excluded.expires_at""",
                (ticker, source, float(size_scale), int(tick_offset),
                 detail or None, now.isoformat(), exp.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log.warning(f"market_throttle.upsert[{ticker}/{source}] failed: {e}")


def active_for(ticker: str,
               db_path: str = settings.DB_PATH) -> tuple[float, int]:
    """Read currently-active throttle for a ticker, combining sources.

    When multiple sources have active rows for the same ticker, we take
    the MIN size_scale (most defensive size cut) and the MAX tick_offset
    (widest back-off). Returns (1.0, 0) when no rows exist or the
    feature flag is off.
    """
    if not getattr(settings, "GRADUATED_THROTTLE", False):
        return (1.0, 0)
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
        try:
            row = conn.execute(
                """SELECT MIN(size_scale), MAX(tick_offset)
                   FROM market_throttle
                   WHERE ticker = ? AND datetime(expires_at) > datetime('now')""",
                (ticker,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return (1.0, 0)
    if row is None or row[0] is None:
        return (1.0, 0)
    return (float(row[0]), int(row[1] or 0))


def _self_test() -> None:
    import os, tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        ensure_schema(path)

        # Tier mapping
        assert vpin_tier(0.40) is None
        assert vpin_tier(0.55)[0] == 0.70 and vpin_tier(0.55)[1] == 0
        assert vpin_tier(0.65)[0] == 0.40 and vpin_tier(0.65)[1] == 1
        assert vpin_tier(0.75)[0] == 0.15 and vpin_tier(0.75)[1] == 2
        assert vpin_tier(0.85)[0] == 0.0

        # Upsert + read (flag must be on)
        upsert("KXBRENTD-X", size_scale=0.4, tick_offset=1,
               source="vpin", ttl_sec=600, detail="vpin=0.65", db_path=path)
        upsert("KXBRENTD-X", size_scale=0.7, tick_offset=2,
               source="toxicity", ttl_sec=600, detail="imb=92%", db_path=path)
        # Flag off → fallback
        assert active_for("KXBRENTD-X", db_path=path) == (1.0, 0)
        # Flip on, expect MIN size_scale + MAX tick_offset across sources
        settings.GRADUATED_THROTTLE = True
        try:
            s, t = active_for("KXBRENTD-X", db_path=path)
            assert s == 0.4 and t == 2, (s, t)
            assert active_for("UNKNOWN-X", db_path=path) == (1.0, 0)
        finally:
            settings.GRADUATED_THROTTLE = False

        print("market_throttle self-test: PASS")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
