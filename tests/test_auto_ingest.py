"""Tests for corpus/auto_ingest.py — auto ingestion pipeline."""

import os
import sys
import sqlite3
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from corpus.auto_ingest import (
    auto_ingest, _get_existing_count, _get_missing_quarters,
    _clean_text, _extract_top_terms,
)

TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_ingest.db")


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    monkeypatch.setattr("corpus.auto_ingest.DB_PATH", TEST_DB)
    monkeypatch.setattr("corpus.validator.DB_PATH", TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY, source TEXT, speaker TEXT, event_type TEXT,
            event_date DATE, raw_text TEXT, word_count INTEGER,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ingestion_log (
            id INTEGER PRIMARY KEY, ticker TEXT, event_type TEXT, triggered_by TEXT,
            quarters_found INTEGER, quarters_ingested INTEGER, quarters_rejected INTEGER,
            corpus_n_before INTEGER, corpus_n_after INTEGER, ready_to_trade BOOLEAN,
            duration_seconds REAL, triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS frequency_matrix (
            id INTEGER PRIMARY KEY, speaker TEXT, event_type TEXT, term TEXT,
            occurrences INTEGER, total_events INTEGER, base_rate REAL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            avg_count_per_mention REAL DEFAULT 0, max_count INTEGER DEFAULT 0, UNIQUE(speaker, event_type, term)
        );
        CREATE TABLE IF NOT EXISTS mentions (
            id INTEGER PRIMARY KEY, transcript_id INTEGER, term TEXT,
            mentioned BOOLEAN, mention_count INTEGER DEFAULT 0,
            context_snippet TEXT, UNIQUE(transcript_id, term)
        );
        CREATE TABLE IF NOT EXISTS cooccurrence (
            id INTEGER PRIMARY KEY, event_type TEXT, term_a TEXT, term_b TEXT,
            both_mentioned INTEGER, a_mentioned INTEGER, p_b_given_a REAL,
            UNIQUE(event_type, term_a, term_b)
        );
        DELETE FROM transcripts;
        DELETE FROM ingestion_log;
    """)
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_unknown_speaker_returns_skip():
    result = auto_ingest("ZZZZZ_FAKE_TICKER")
    assert result["status"] == "unknown_speaker"
    assert result["ready_to_trade"] is False


def test_get_existing_count():
    conn = sqlite3.connect(TEST_DB)
    for i in range(3):
        conn.execute(
            "INSERT INTO transcripts (source,speaker,event_type,event_date,raw_text,word_count) VALUES (?,?,?,?,?,?)",
            ("earnings", "musk", "tsla_earnings", f"2024-0{i+1}-15", "test", 100)
        )
    conn.commit()
    conn.close()
    assert _get_existing_count("tsla_earnings") == 3


def test_clean_text_removes_whitespace():
    raw = "Hello\n\n\n\n\nWorld   with   spaces"
    cleaned = _clean_text(raw)
    assert "\n\n\n" not in cleaned
    assert "   " not in cleaned


def test_extract_top_terms_filters_stopwords():
    conn = sqlite3.connect(TEST_DB)
    for i in range(3):
        conn.execute(
            "INSERT INTO transcripts (source,speaker,event_type,event_date,raw_text,word_count) VALUES (?,?,?,?,?,?)",
            ("earnings", "test", "test_earnings", f"2024-0{i+1}-15",
             "Revenue growth was strong. Margins improved. Revenue beat expectations. Guidance raised. " * 20,
             100)
        )
    conn.commit()
    conn.close()
    terms = _extract_top_terms("test_earnings", top_n=10)
    assert "the" not in terms
    assert "and" not in terms
    assert len(terms) > 0


def test_get_missing_quarters_excludes_existing():
    conn = sqlite3.connect(TEST_DB)
    # Insert a transcript dated recently
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO transcripts (source,speaker,event_type,event_date,raw_text,word_count) VALUES (?,?,?,?,?,?)",
        ("earnings", "musk", "tsla_earnings", recent, "test", 100)
    )
    conn.commit()
    conn.close()
    quarters = _get_missing_quarters("tsla_earnings")
    # Should not include the quarter we already have
    dates = [q["approx_date"] for q in quarters]
    for d in dates:
        from datetime import date as d_cls
        assert abs((d_cls.fromisoformat(d) - d_cls.fromisoformat(recent)).days) > 45
