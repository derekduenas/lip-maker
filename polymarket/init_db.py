"""Initialize Polymarket maker SQLite schema.

Mirrors lip-maker schema but adapted for Polymarket's per-minute
weekly-epoch scoring model.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from config import settings


PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
"""


SCHEMA = """
-- Markets we track (Polymarket equivalent of lip_programs)
CREATE TABLE IF NOT EXISTS pm_markets (
    market_slug              TEXT PRIMARY KEY,        -- human-readable, e.g. 'btc-100k-2025'
    series_ticker            TEXT,                     -- inferred prefix
    question                 TEXT,
    end_date                 TEXT,                     -- market close UTC
    epoch_start              TEXT,                     -- weekly epoch start
    epoch_end                TEXT,                     -- weekly epoch end
    period_reward_usd        REAL,                     -- weekly pool size USD
    reward_per_day_usd       REAL,                     -- normalized
    max_spread               REAL,                     -- per-market scoring param
    min_size                 REAL,                     -- per-market scoring param
    b_size_weight            REAL,                     -- 'b' in S(v,s) formula
    enrolled                 INTEGER NOT NULL DEFAULT 0,
    blocked_reason           TEXT,
    last_seen                TEXT NOT NULL,
    UNIQUE(market_slug, epoch_start)
);
CREATE INDEX IF NOT EXISTS idx_pm_market  ON pm_markets(market_slug);
CREATE INDEX IF NOT EXISTS idx_pm_series  ON pm_markets(series_ticker);
CREATE INDEX IF NOT EXISTS idx_pm_enrol   ON pm_markets(enrolled);

-- Per-minute snapshot scoring (analogous to lip_snapshots)
CREATE TABLE IF NOT EXISTS pm_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug     TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    midpoint        REAL,
    yes_bid_size    REAL,
    yes_ask_size    REAL,
    no_bid_size     REAL,
    no_ask_size     REAL,
    our_yes_score   REAL,                              -- our spread score yes side
    our_no_score    REAL,                              -- our spread score no side
    q_min           REAL,                              -- two-sided gate output
    total_q_min     REAL,                              -- sum across all LPs (estimated)
    snapshot_valid  INTEGER NOT NULL,                  -- both sides qualified
    estimated_payout_usd REAL,
    was_resting     INTEGER                            -- did we have actual orders resting
);
CREATE INDEX IF NOT EXISTS idx_pm_snap_market ON pm_snapshots(market_slug);
CREATE INDEX IF NOT EXISTS idx_pm_snap_time   ON pm_snapshots(captured_at);

-- Public orderbook snapshots — collected by public_recorder (no auth)
CREATE TABLE IF NOT EXISTS pm_book_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug     TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    yes_bids_json   TEXT NOT NULL,
    yes_asks_json   TEXT NOT NULL,
    no_bids_json    TEXT,
    no_asks_json    TEXT,
    best_yes_bid    REAL,
    best_yes_ask    REAL,
    best_no_bid     REAL,
    best_no_ask     REAL,
    midpoint        REAL
);
CREATE INDEX IF NOT EXISTS idx_pm_book_market ON pm_book_log(market_slug, captured_at DESC);

-- Our orders
CREATE TABLE IF NOT EXISTS pm_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug     TEXT NOT NULL,
    side            TEXT NOT NULL,          -- 'yes_bid' | 'yes_ask' | 'no_bid' | 'no_ask'
    price           REAL NOT NULL,
    size_contracts  INTEGER NOT NULL,
    order_id        TEXT,                   -- Polymarket order id, or PAPER-* in paper mode
    status          TEXT NOT NULL,          -- 'pending'|'resting'|'filled'|'cancelled'|'rejected'
    paper           INTEGER NOT NULL DEFAULT 1,
    placed_at       TEXT NOT NULL,
    cancelled_at    TEXT,
    filled_at       TEXT,
    fill_size       INTEGER,
    fill_price      REAL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_pm_orders_market ON pm_orders(market_slug);
CREATE INDEX IF NOT EXISTS idx_pm_orders_status ON pm_orders(status);

-- Fill ledger
CREATE TABLE IF NOT EXISTS pm_fill_ledger (
    trade_id          TEXT PRIMARY KEY,
    order_id          TEXT NOT NULL,
    market_slug       TEXT NOT NULL,
    side              TEXT NOT NULL,
    count             INTEGER NOT NULL,
    price             REAL NOT NULL,
    is_taker          INTEGER,
    created_at        TEXT NOT NULL,
    synced_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_fill_market ON pm_fill_ledger(market_slug);
CREATE INDEX IF NOT EXISTS idx_pm_fill_order  ON pm_fill_ledger(order_id);

-- Settlement / payout history (daily USDC drops at 00:00 UTC)
CREATE TABLE IF NOT EXISTS pm_payouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch_end       TEXT NOT NULL,
    market_slug     TEXT,                              -- null = aggregate row
    payout_usd      REAL NOT NULL,
    snapshot_at     TEXT NOT NULL,
    UNIQUE(epoch_end, market_slug)
);

-- Daily PnL log
CREATE TABLE IF NOT EXISTS pm_daily_pnl_log (
    day                  TEXT PRIMARY KEY,
    realized_pnl_usd     REAL NOT NULL,
    daily_realized_delta REAL,
    open_exposure_usd    REAL,
    open_positions       INTEGER,
    total_fills_to_date  INTEGER,
    balance_usd          REAL,
    snapshot_at          TEXT NOT NULL
);

-- Market blacklist
CREATE TABLE IF NOT EXISTS pm_market_blacklist (
    market_slug  TEXT PRIMARY KEY,
    expires_at   TEXT NOT NULL,
    reason       TEXT NOT NULL,
    added_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_bl_expires ON pm_market_blacklist(expires_at);
"""


def init_db(db_path: str = settings.DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(PRAGMAS)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    print(f"initialized {db_path}")


if __name__ == "__main__":
    init_db()
