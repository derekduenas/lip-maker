"""Initialize LIP maker SQLite schema."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from config import settings


# Audit #14: WAL mode lets readers (dashboard) and writers (quote loop,
# heartbeat, timers) work concurrently instead of blocking each other.
# Applied to fresh DBs via init_db; applied to existing DB 2026-04-21 manually.
PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
"""


SCHEMA = """
-- LIP programs we're tracking
CREATE TABLE IF NOT EXISTS lip_programs (
    id                       TEXT PRIMARY KEY,             -- Kalshi program id
    market_ticker            TEXT NOT NULL,
    series_ticker            TEXT,
    start_date               TEXT NOT NULL,
    end_date                 TEXT NOT NULL,
    period_reward_usd        REAL NOT NULL,               -- dollars (converted from centi-cents)
    discount_factor          REAL NOT NULL,               -- 0.0–1.0 (converted from bps)
    target_size              REAL NOT NULL,
    paid_out                 INTEGER NOT NULL DEFAULT 0,  -- 0/1
    enrolled                 INTEGER NOT NULL DEFAULT 0,  -- 0/1 our decision to quote
    blocked_reason           TEXT,                         -- e.g. "target_too_large"
    reward_per_day_usd       REAL,                         -- period_reward / days_in_period
    last_seen                TEXT NOT NULL,
    UNIQUE(market_ticker, start_date)
);

CREATE INDEX IF NOT EXISTS idx_lip_market  ON lip_programs(market_ticker);
CREATE INDEX IF NOT EXISTS idx_lip_series  ON lip_programs(series_ticker);
CREATE INDEX IF NOT EXISTS idx_lip_enrol   ON lip_programs(enrolled);

-- Our quotes — placed / modified / cancelled
CREATE TABLE IF NOT EXISTS quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_ticker   TEXT NOT NULL,
    side            TEXT NOT NULL,          -- 'yes' | 'no'
    price_cents     INTEGER NOT NULL,
    size_contracts  INTEGER NOT NULL,
    order_id        TEXT,                   -- Kalshi order id when live, or SHADOW-* in paper
    status          TEXT NOT NULL,          -- 'pending'|'resting'|'filled'|'cancelled'|'rejected'
    paper           INTEGER NOT NULL DEFAULT 1,
    placed_at       TEXT NOT NULL,
    cancelled_at    TEXT,
    filled_at       TEXT,
    fill_size       INTEGER,
    fill_price_cents INTEGER,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_quotes_market ON quotes(market_ticker);
CREATE INDEX IF NOT EXISTS idx_quotes_status ON quotes(status);

-- Simulated LIP snapshots — what we think we earned each second
CREATE TABLE IF NOT EXISTS lip_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_ticker   TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    our_score       REAL NOT NULL,      -- our normalized share this snapshot (0.0–2.0)
    total_score     REAL NOT NULL,
    yes_qualified   INTEGER,            -- did total yes meet TargetSize
    no_qualified    INTEGER,            -- did total no meet TargetSize
    snapshot_valid  INTEGER NOT NULL,   -- both sides qualified (post-Feb28 rule)
    estimated_payout_usd REAL           -- our_score × (period_reward × fraction_of_period)
);

CREATE INDEX IF NOT EXISTS idx_snap_market ON lip_snapshots(market_ticker);
CREATE INDEX IF NOT EXISTS idx_snap_time   ON lip_snapshots(captured_at);

-- Orderbook deltas / snapshots log (tiny — keep 1 hour rolling)
CREATE TABLE IF NOT EXISTS book_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_ticker   TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    yes_bids_json   TEXT NOT NULL,   -- [[price_cents, size], ...]
    yes_asks_json   TEXT NOT NULL,
    best_yes_bid    INTEGER,
    best_yes_ask    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_book_market ON book_log(market_ticker, captured_at DESC);

-- Inventory tracker — reconciled from fills
CREATE TABLE IF NOT EXISTS inventory (
    market_ticker   TEXT PRIMARY KEY,
    net_yes_contracts INTEGER NOT NULL DEFAULT 0,  -- signed; positive = long yes
    gross_usd       REAL NOT NULL DEFAULT 0.0,
    avg_yes_entry   REAL,
    avg_no_entry    REAL,
    last_updated    TEXT NOT NULL
);

-- Reconciliation: end-of-period actual payout vs our simulation
CREATE TABLE IF NOT EXISTS period_reconciliation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    market_ticker   TEXT NOT NULL,
    our_simulated_usd REAL NOT NULL,
    kalshi_actual_usd REAL,          -- filled by balance-diff at period end
    recorded_at     TEXT NOT NULL
);
"""


def init_db(db_path: str = settings.DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(PRAGMAS)  # WAL + busy_timeout (audit #14)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    print(f"initialized {db_path}")


if __name__ == "__main__":
    init_db()
