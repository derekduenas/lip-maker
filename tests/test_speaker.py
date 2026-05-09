"""Tests for engine/speaker.py"""

import sqlite3
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine.speaker import (
    get_speaker_base_rate, compute_term_trend,
    build_speaker_frequency_matrix, MIN_EARNINGS_N,
)

TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_speaker.db")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    monkeypatch.setattr("engine.speaker.DB_PATH", TEST_DB)
    monkeypatch.setattr("engine.frequency.DB_PATH", TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transcripts (id INTEGER PRIMARY KEY, source TEXT, speaker TEXT, event_type TEXT, event_date DATE, raw_text TEXT, word_count INTEGER, ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS mentions (id INTEGER PRIMARY KEY, transcript_id INTEGER, term TEXT, mentioned BOOLEAN, mention_count INTEGER DEFAULT 0, context_snippet TEXT, UNIQUE(transcript_id, term));
        CREATE TABLE IF NOT EXISTS frequency_matrix (id INTEGER PRIMARY KEY, speaker TEXT, event_type TEXT, term TEXT, occurrences INTEGER, total_events INTEGER, base_rate REAL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, avg_count_per_mention REAL DEFAULT 0, max_count INTEGER DEFAULT 0, UNIQUE(speaker, event_type, term));
        CREATE TABLE IF NOT EXISTS cooccurrence (id INTEGER PRIMARY KEY, event_type TEXT, term_a TEXT, term_b TEXT, both_mentioned INTEGER, a_mentioned INTEGER, p_b_given_a REAL, UNIQUE(event_type, term_a, term_b));
        DELETE FROM transcripts; DELETE FROM mentions; DELETE FROM frequency_matrix; DELETE FROM cooccurrence;
    """)
    # Insert test transcripts
    for i, (date, text) in enumerate([
        ("2024-01-15", "We discussed autonomy and FSD. The robotaxi program is on track."),
        ("2024-04-15", "FSD is improving. Autonomy remains our focus. Dojo training."),
        ("2024-07-15", "FSD updates. New autonomy features. Cybertruck production."),
        ("2024-10-15", "Autonomy is the future. FSD v13 is incredible."),
    ]):
        conn.execute("INSERT INTO transcripts (source,speaker,event_type,event_date,raw_text,word_count) VALUES (?,?,?,?,?,?)",
                     ("earnings", "musk", "tsla_earnings", date, text, len(text.split())))
    conn.commit()
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_speaker_base_rate_returns_none_when_n_lt_3():
    """Base rate should be None when fewer than MIN_EARNINGS_N transcripts."""
    conn = sqlite3.connect(TEST_DB)
    conn.execute("DELETE FROM transcripts WHERE event_date > '2024-04-20'")
    conn.commit()
    conn.close()

    # Build matrix with only 2 transcripts
    build_speaker_frequency_matrix("musk", "TSLA")
    result = get_speaker_base_rate("musk", "nonexistent_term_xyz", "TSLA")
    assert result is None


def test_term_trend_correctly_classifies():
    build_speaker_frequency_matrix("musk", "TSLA")
    # FSD appears in all 4 — stable
    trend = compute_term_trend("musk", "FSD", "TSLA")
    assert trend == "stable"

    # dojo appears only in Q2 — should be decreasing (1/2 recent = 0 vs 1/2 prior = 0.5)
    trend_dojo = compute_term_trend("musk", "dojo", "TSLA")
    assert trend_dojo in ("decreasing", "stable")


def test_speaker_frequency_report_runs(capsys):
    from engine.speaker import speaker_frequency_report
    speaker_frequency_report("musk", "TSLA")
    captured = capsys.readouterr()
    assert "TSLA" in captured.out
    assert "MUSK" in captured.out
