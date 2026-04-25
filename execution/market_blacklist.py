"""Per-market blacklist — auto-blocks markets after repeated losses.

Rule: if a market produces ≥2 losing fills in any 24h window, block quoting
on that market for 24h. Prevents bleed from specific adverse-selection hotspots.

Table `market_blacklist`:
    ticker       — blocked market
    expires_at   — ISO timestamp when block lifts
    reason       — "auto_2_losses_24h" | "manual" | other
    added_at     — when block created

Usage from quote_manager:
    if is_blocked(ticker):
        return False, "market_blocked"

Usage from daily cron:
    update_blacklist_from_losses(db_path)  # recompute auto-blocks
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_blacklist (
    ticker       TEXT PRIMARY KEY,
    expires_at   TEXT NOT NULL,
    reason       TEXT NOT NULL,
    added_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bl_expires ON market_blacklist(expires_at);
"""


def init_schema(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def is_blocked(ticker: str, db_path: str) -> tuple[bool, Optional[str]]:
    """Returns (blocked, reason_or_none). Caches expiration check against `now`."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                """SELECT expires_at, reason FROM market_blacklist
                   WHERE ticker = ? AND expires_at > ?""",
                (ticker, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
            return (True, row[1]) if row else (False, None)
        finally:
            conn.close()
    except Exception:
        return (False, None)  # if DB hiccup, don't block (fail-open)


def add_block(ticker: str, reason: str, hours: int = 24, db_path: str = None):
    """Insert or extend a block."""
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=hours)).isoformat()
    init_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO market_blacklist (ticker, expires_at, reason, added_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 expires_at = excluded.expires_at,
                 reason     = excluded.reason,
                 added_at   = excluded.added_at""",
            (ticker, expires, reason, now.isoformat()),
        )
        conn.commit()
        _log.info(f"[BLACKLIST] added {ticker} until {expires} — {reason}")
    finally:
        conn.close()


def update_blacklist_from_losses(db_path: str, loss_threshold: int = 2,
                                  window_hours: int = 24) -> list[str]:
    """Scan recent losing fills, auto-blacklist tickers with ≥threshold losses.
    Returns list of newly-blocked tickers."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT market_ticker, COUNT(*) AS losses
               FROM quotes
               WHERE status = 'filled'
                 AND filled_at >= ?
                 AND fill_price_cents IS NOT NULL
                 AND fill_price_cents < price_cents
               GROUP BY market_ticker
               HAVING COUNT(*) >= ?""",
            (cutoff, loss_threshold),
        ).fetchall()
    finally:
        conn.close()

    newly_blocked = []
    for ticker, n_losses in rows:
        blocked, _ = is_blocked(ticker, db_path)
        if blocked:
            continue
        add_block(ticker, f"auto_{n_losses}_losses_{window_hours}h",
                  hours=24, db_path=db_path)
        newly_blocked.append(ticker)
    return newly_blocked


def list_active_blocks(db_path: str) -> list[tuple[str, str, str]]:
    """Returns [(ticker, expires_at, reason), ...] for all active blocks."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT ticker, expires_at, reason FROM market_blacklist
               WHERE expires_at > ? ORDER BY expires_at DESC""",
            (datetime.now(timezone.utc).isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return rows


def cleanup_expired(db_path: str) -> int:
    """Remove rows whose expiry is past. Returns count deleted."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM market_blacklist WHERE expires_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from config import settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    init_schema(settings.DB_PATH)
    blocked = update_blacklist_from_losses(settings.DB_PATH)
    print(f"newly blocked: {blocked}")
    cleaned = cleanup_expired(settings.DB_PATH)
    print(f"expired cleaned: {cleaned}")
    print(f"\nActive blocks:")
    for t, e, r in list_active_blocks(settings.DB_PATH):
        print(f"  {t:40s} until {e[:19]}  {r}")
