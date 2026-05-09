"""Initialize sovereign.db with the full schema."""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "sovereign.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    speaker         TEXT,
    event_type      TEXT NOT NULL,
    event_date      DATE NOT NULL,
    raw_text        TEXT NOT NULL,
    word_count      INTEGER,
    ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mentions (
    id              INTEGER PRIMARY KEY,
    transcript_id   INTEGER REFERENCES transcripts(id),
    term            TEXT NOT NULL,
    mentioned       BOOLEAN NOT NULL,
    mention_count   INTEGER DEFAULT 0,
    context_snippet TEXT,
    UNIQUE(transcript_id, term)
);

CREATE TABLE IF NOT EXISTS frequency_matrix (
    id              INTEGER PRIMARY KEY,
    speaker         TEXT,
    event_type      TEXT NOT NULL,
    term            TEXT NOT NULL,
    occurrences     INTEGER NOT NULL,
    total_events    INTEGER NOT NULL,
    base_rate       REAL NOT NULL,
    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(speaker, event_type, term)
);

CREATE TABLE IF NOT EXISTS cooccurrence (
    id              INTEGER PRIMARY KEY,
    event_type      TEXT NOT NULL,
    term_a          TEXT NOT NULL,
    term_b          TEXT NOT NULL,
    both_mentioned  INTEGER NOT NULL,
    a_mentioned     INTEGER NOT NULL,
    p_b_given_a     REAL NOT NULL,
    UNIQUE(event_type, term_a, term_b)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id              INTEGER PRIMARY KEY,
    kalshi_market_id TEXT NOT NULL,
    ticker          TEXT,
    term            TEXT NOT NULL,
    event_type      TEXT,
    event_date      DATE,
    yes_price       REAL NOT NULL,
    volume          INTEGER,
    snapshot_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunities (
    id              INTEGER PRIMARY KEY,
    kalshi_market_id TEXT NOT NULL,
    term            TEXT NOT NULL,
    base_rate       REAL,
    context_score   REAL,
    estimated_prob  REAL NOT NULL,
    market_price    REAL NOT NULL,
    edge            REAL NOT NULL,
    kelly_fraction  REAL,
    parlay_flag     BOOLEAN DEFAULT FALSE,
    detected_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY,
    opportunity_id  INTEGER REFERENCES opportunities(id),
    kalshi_market_id TEXT NOT NULL,
    term            TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    price           REAL NOT NULL,
    contracts       INTEGER NOT NULL,
    total_cost      REAL NOT NULL,
    fee_paid        REAL,
    paper           BOOLEAN DEFAULT TRUE,
    placed_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TIMESTAMP,
    outcome         TEXT,
    pnl             REAL
);

CREATE TABLE IF NOT EXISTS strategy_scores (
    id              INTEGER PRIMARY KEY,
    strategy        TEXT NOT NULL,
    trades          INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    total_pnl       REAL DEFAULT 0,
    avg_roi         REAL,
    recorded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id              INTEGER PRIMARY KEY,
    ticker          TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    triggered_by    TEXT NOT NULL,
    quarters_found  INTEGER DEFAULT 0,
    quarters_ingested INTEGER DEFAULT 0,
    quarters_rejected INTEGER DEFAULT 0,
    corpus_n_before INTEGER DEFAULT 0,
    corpus_n_after  INTEGER DEFAULT 0,
    ready_to_trade  BOOLEAN DEFAULT FALSE,
    duration_seconds REAL,
    triggered_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transcript_quality (
    id              INTEGER PRIMARY KEY,
    transcript_id   INTEGER REFERENCES transcripts(id),
    source          TEXT NOT NULL,
    quality_score   REAL NOT NULL,
    word_count      INTEGER NOT NULL,
    speaker_changes INTEGER NOT NULL,
    warnings        TEXT,
    validated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.close()
    print(f"Database initialized at {DB_PATH}")

if __name__ == "__main__":
    init_db()
