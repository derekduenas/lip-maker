"""K4: AdaptiveSizer uses realized 7d share as per-market target.

Tests:
- update_target_shares_from_db reads lip_snapshots and sets per-market target
- target_share is clamped to [lo, hi]
- size_for uses per-market target when set, falls back to global default
- Markets with <10 samples are skipped (not enough signal)
"""
import sqlite3
import pytest


def _seed_snapshots(db, ticker, share, n):
    """Insert n snapshots with given share = our_score / total_score."""
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lip_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_ticker TEXT, captured_at TEXT,
            our_score REAL, total_score REAL,
            yes_qualified INTEGER, no_qualified INTEGER,
            snapshot_valid INTEGER NOT NULL,
            estimated_payout_usd REAL, was_resting INTEGER DEFAULT 0
        );
    """)
    total = 100.0
    our = share * total
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for _ in range(n):
        conn.execute(
            "INSERT INTO lip_snapshots (market_ticker, captured_at, our_score, "
            "total_score, snapshot_valid) VALUES (?, ?, ?, ?, 1)",
            (ticker, now, our, total),
        )
    conn.commit()
    conn.close()


def test_realized_share_clamps_to_range(tmp_path):
    from engine.adaptive_sizer import AdaptiveSizer
    db = tmp_path / "snap.db"

    # Market A: realized 5% share (well below clamp lo=0.10)
    _seed_snapshots(str(db), "MKT-A", share=0.05, n=15)
    # Market B: realized 30% share (within range)
    _seed_snapshots(str(db), "MKT-B", share=0.30, n=15)
    # Market C: realized 80% share (above clamp hi=0.50)
    _seed_snapshots(str(db), "MKT-C", share=0.80, n=15)

    sizer = AdaptiveSizer(target_share=0.35)
    n = sizer.update_target_shares_from_db(str(db), days=7, lo=0.10, hi=0.50)

    assert n == 3
    assert sizer.per_market_target["MKT-A"] == 0.10  # clamped to lo
    assert sizer.per_market_target["MKT-B"] == 0.30  # within
    assert sizer.per_market_target["MKT-C"] == 0.50  # clamped to hi


def test_low_sample_markets_skipped(tmp_path):
    from engine.adaptive_sizer import AdaptiveSizer
    db = tmp_path / "snap.db"
    _seed_snapshots(str(db), "MKT-THIN", share=0.20, n=5)  # only 5 samples

    sizer = AdaptiveSizer(target_share=0.35)
    n = sizer.update_target_shares_from_db(str(db), days=7)

    assert n == 0
    assert "MKT-THIN" not in sizer.per_market_target
    # Falls back to global default
    assert sizer.get_target_share("MKT-THIN") == 0.35


def test_get_target_share_uses_per_market_when_set(tmp_path):
    from engine.adaptive_sizer import AdaptiveSizer
    sizer = AdaptiveSizer(target_share=0.35)
    sizer.per_market_target["MKT-X"] = 0.12

    assert sizer.get_target_share("MKT-X") == 0.12
    assert sizer.get_target_share("MKT-OTHER") == 0.35  # default


def test_no_db_or_no_table_returns_zero(tmp_path):
    from engine.adaptive_sizer import AdaptiveSizer
    sizer = AdaptiveSizer(target_share=0.35)

    # Path doesn't exist
    n = sizer.update_target_shares_from_db(str(tmp_path / "nonexistent.db"))
    assert n == 0

    # Path exists but no table
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    n = sizer.update_target_shares_from_db(str(db))
    assert n == 0
