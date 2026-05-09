"""Test prep/corpus_compiler.py — DB queries, before_date filter, summary."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.corpus_compiler import CorpusPackage, compile_for_event


def _make_test_db() -> str:
    """Create a temp SQLite with some transcripts."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,
            source TEXT, speaker TEXT, event_type TEXT, event_date DATE,
            raw_text TEXT, word_count INTEGER,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    samples = [
        (1, "fed.gov", "powell", "fomc_presser", "2026-03-18", "text march 2026", 500),
        (2, "fed.gov", "powell", "fomc_presser", "2026-01-28", "text jan 2026", 500),
        (3, "fed.gov", "powell", "fomc_presser", "2025-12-17", "text dec 2025", 500),
        (4, "fed.gov", "powell", "fomc_presser", "2025-11-05", "text nov 2025", 500),
        (5, "fed.gov", "fed", "fomc_statement", "2026-03-18", "stmt march", 200),
        (6, "fed.gov", "fed", "fomc_statement", "2026-01-28", "stmt jan", 200),
        (7, "fed.gov", "fed", "fomc_minutes", "2026-03-18", "minutes march", 800),
        (8, "fed.gov", "musk", "tsla_earnings", "2026-01-28", "earnings", 1000),
    ]
    conn.executemany(
        "INSERT INTO transcripts (id, source, speaker, event_type, event_date, "
        "raw_text, word_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        samples,
    )
    conn.commit()
    conn.close()
    return path


def test_compile_returns_package():
    db = _make_test_db()
    try:
        p = compile_for_event(
            event_id="fomc_test", event_type="fomc_presser",
            event_date_iso="2026-04-30", db_path=db, prior_n=10,
        )
        assert isinstance(p, CorpusPackage)
        assert p.event_id == "fomc_test"
        assert p.event_type == "fomc_presser"
    finally:
        os.unlink(db)


def test_compile_excludes_events_on_or_after_event_date():
    """No lookahead — only transcripts BEFORE event_date_iso."""
    db = _make_test_db()
    try:
        # Event date = 2026-03-18 → should exclude the 2026-03-18 presser itself
        p = compile_for_event(
            event_id="fomc_test", event_type="fomc_presser",
            event_date_iso="2026-03-18", db_path=db, prior_n=10,
        )
        dates = [t["event_date"] for t in p.prior_transcripts]
        assert "2026-03-18" not in dates
        assert "2026-01-28" in dates
    finally:
        os.unlink(db)


def test_compile_sorts_oldest_first():
    db = _make_test_db()
    try:
        p = compile_for_event(
            event_id="fomc_test", event_type="fomc_presser",
            event_date_iso="2026-04-30", db_path=db, prior_n=10,
        )
        dates = [t["event_date"] for t in p.prior_transcripts]
        assert dates == sorted(dates), "prior_transcripts should be oldest→newest"
    finally:
        os.unlink(db)


def test_compile_recent_is_newest_first_limited():
    db = _make_test_db()
    try:
        p = compile_for_event(
            event_id="fomc_test", event_type="fomc_presser",
            event_date_iso="2026-04-30", db_path=db, prior_n=10, recent_n=2,
        )
        assert len(p.recent_transcripts) == 2
        assert p.recent_transcripts[0]["event_date"] >= p.recent_transcripts[1]["event_date"]
    finally:
        os.unlink(db)


def test_compile_fomc_includes_statements_and_minutes():
    db = _make_test_db()
    try:
        p = compile_for_event(
            event_id="fomc_test", event_type="fomc_presser",
            event_date_iso="2026-04-30", db_path=db, prior_n=10,
        )
        assert len(p.statements) >= 1
        assert len(p.minutes) >= 1
    finally:
        os.unlink(db)


def test_compile_non_fomc_does_not_include_statements():
    db = _make_test_db()
    try:
        p = compile_for_event(
            event_id="tsla_test", event_type="tsla_earnings",
            event_date_iso="2026-04-30", db_path=db, prior_n=10,
        )
        # Non-FOMC events don't pull statements/minutes
        assert p.statements == []
        assert p.minutes == []
    finally:
        os.unlink(db)


def test_summary_structure():
    db = _make_test_db()
    try:
        p = compile_for_event(
            event_id="fomc_test", event_type="fomc_presser",
            event_date_iso="2026-04-30", db_path=db, prior_n=10,
        )
        s = p.summary()
        assert s["event_id"] == "fomc_test"
        assert s["corpus_count"] >= 1
        assert "prior_n" in s
    finally:
        os.unlink(db)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
