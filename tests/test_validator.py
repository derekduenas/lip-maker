"""Tests for corpus/validator.py — quality gates."""

import os
import sys
import sqlite3
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from corpus.validator import validate_transcript, check_duplicate, log_rejection

TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_validator.db")


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    monkeypatch.setattr("corpus.validator.DB_PATH", TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY, source TEXT, speaker TEXT, event_type TEXT,
            event_date DATE, raw_text TEXT, word_count INTEGER
        );
        DELETE FROM transcripts;
    """)
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def _make_transcript(words=5000):
    """Generate a plausible fake transcript."""
    base = ("Thank you operator. Good afternoon everyone. "
            "Revenue this quarter was strong. Earnings per share beat estimates. "
            "We have forward-looking statements to share. "
            "JOHN SMITH: Let me discuss the question about guidance. "
            "CEO JONES: Our margin improved significantly. "
            "ANALYST: What about the competitive landscape? "
            "OPERATOR: Our next question comes from Goldman Sachs. ")
    return (base * (words // len(base.split()) + 1))[:words * 6]


def test_valid_transcript_passes():
    text = _make_transcript(5000)
    r = validate_transcript(text, "motley_fool", "TSLA", "Q1 2025")
    assert r.passed is True
    assert r.quality_score > 0


def test_too_short_rejected():
    r = validate_transcript("short text", "motley_fool", "TSLA", "Q1 2025")
    assert r.passed is False
    assert "Too short" in r.failure_reason


def test_forbidden_phrase_rejected():
    text = _make_transcript(3000) + " page not found error "
    r = validate_transcript(text, "motley_fool", "TSLA", "Q1 2025")
    assert r.passed is False
    assert "Forbidden" in r.failure_reason


def test_paywall_phrase_rejected():
    text = _make_transcript(3000) + " subscribe to read the full article "
    r = validate_transcript(text, "motley_fool", "TSLA", "Q1 2025")
    assert r.passed is False
    assert "Forbidden" in r.failure_reason


def test_duplicate_rejected():
    text = _make_transcript(3000)
    conn = sqlite3.connect(TEST_DB)
    conn.execute(
        "INSERT INTO transcripts (source,speaker,event_type,event_date,raw_text,word_count) VALUES (?,?,?,?,?,?)",
        ("earnings", "musk", "tsla_earnings", "2025-01-01", text, len(text.split()))
    )
    conn.commit()
    conn.close()

    r = validate_transcript(text, "motley_fool", "TSLA", "Q1 2025")
    assert r.passed is False
    assert "Duplicate" in r.failure_reason


def test_quality_score_range():
    text = _make_transcript(8000)
    r = validate_transcript(text, "motley_fool", "TSLA", "Q1 2025")
    assert 0.0 <= r.quality_score <= 1.0


def test_log_rejection_writes(tmp_path, monkeypatch):
    log_file = str(tmp_path / "rejected.log")
    monkeypatch.setattr("corpus.validator.REJECTED_LOG", log_file)
    log_rejection("TSLA", "Q1 2025", "motley_fool", "test reason")
    assert os.path.exists(log_file)
    with open(log_file) as f:
        content = f.read()
    assert "test reason" in content
