"""Tests for engine/frequency.py — extraction, base rates, co-occurrence, parlay edge."""

import sqlite3
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.frequency import (
    extract_mentions,
    build_frequency_matrix,
    get_base_rate,
    build_cooccurrence_matrix,
    get_conditional_prob,
    detect_parlay_edge,
)
from config.settings import MIN_BASE_RATE_N

# Test database path
TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_sovereign.db")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """Create a clean test database for each test."""
    # Patch DB_PATH
    monkeypatch.setattr("engine.frequency.DB_PATH", TEST_DB)

    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY, source TEXT, speaker TEXT,
            event_type TEXT, event_date DATE, raw_text TEXT,
            word_count INTEGER, ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS mentions (
            id INTEGER PRIMARY KEY, transcript_id INTEGER,
            term TEXT, mentioned BOOLEAN, mention_count INTEGER DEFAULT 0,
            context_snippet TEXT, UNIQUE(transcript_id, term)
        );
        CREATE TABLE IF NOT EXISTS frequency_matrix (
            id INTEGER PRIMARY KEY, speaker TEXT, event_type TEXT,
            term TEXT, occurrences INTEGER, total_events INTEGER,
            base_rate REAL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            avg_count_per_mention REAL DEFAULT 0, max_count INTEGER DEFAULT 0, UNIQUE(speaker, event_type, term)
        );
        CREATE TABLE IF NOT EXISTS cooccurrence (
            id INTEGER PRIMARY KEY, event_type TEXT,
            term_a TEXT, term_b TEXT, both_mentioned INTEGER,
            a_mentioned INTEGER, p_b_given_a REAL,
            UNIQUE(event_type, term_a, term_b)
        );
        DELETE FROM transcripts;
        DELETE FROM mentions;
        DELETE FROM frequency_matrix;
        DELETE FROM cooccurrence;
    """)
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


# --- extract_mentions tests ---

def test_extract_mentions_basic():
    text = "Inflation remains elevated. The labor market is strong. We see disinflation."
    terms = ["inflation", "labor market", "recession", "disinflation"]
    result = extract_mentions(text, terms)

    assert result["inflation"]["mentioned"] is True
    assert result["inflation"]["count"] >= 1
    assert result["labor market"]["mentioned"] is True
    assert result["recession"]["mentioned"] is False
    assert result["recession"]["count"] == 0
    assert result["disinflation"]["mentioned"] is True


def test_extract_mentions_case_insensitive():
    text = "INFLATION is a concern. Inflation targets remain."
    result = extract_mentions(text, ["inflation"])
    assert result["inflation"]["mentioned"] is True
    assert result["inflation"]["count"] == 2


def test_extract_mentions_word_boundary():
    text = "The nation is strong. We see no stagnation."
    result = extract_mentions(text, ["inflation", "stagflation"])
    assert result["inflation"]["mentioned"] is False
    assert result["stagflation"]["mentioned"] is False


def test_extract_mentions_context_snippets():
    text = "The economy is growing. Inflation remains at 3 percent. Growth is solid."
    result = extract_mentions(text, ["inflation"])
    assert len(result["inflation"]["context_snippets"]) >= 1
    assert "3 percent" in result["inflation"]["context_snippets"][0]


# --- build_frequency_matrix + get_base_rate tests ---

def _insert_test_transcripts(n: int, term_present_in: list[int], term: str = "inflation"):
    """Insert n test transcripts. term appears in transcripts at indices in term_present_in."""
    conn = sqlite3.connect(TEST_DB)
    for i in range(n):
        if i in term_present_in:
            text = f"Transcript {i}. We discussed {term} at length today."
        else:
            text = f"Transcript {i}. The economy is doing well. No special terms here."
        conn.execute(
            "INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count) VALUES (?,?,?,?,?,?)",
            ("test", "speaker", "test_event", f"2024-01-{i+1:02d}", text, len(text.split()))
        )
    conn.commit()
    conn.close()


def test_build_frequency_matrix_correct_rate():
    # 10 transcripts, "inflation" appears in 7
    _insert_test_transcripts(10, [0, 1, 2, 3, 5, 7, 9])
    build_frequency_matrix("test_event", ["inflation"])
    rate = get_base_rate("test_event", "inflation")
    assert rate == pytest.approx(0.7, abs=0.01)


def test_get_base_rate_none_below_min():
    # Only 3 transcripts — below MIN_BASE_RATE_N (5)
    _insert_test_transcripts(3, [0, 1])
    build_frequency_matrix("test_event", ["inflation"])
    rate = get_base_rate("test_event", "inflation")
    assert rate is None


def test_get_base_rate_returns_value_at_min():
    # Exactly MIN_BASE_RATE_N transcripts
    _insert_test_transcripts(MIN_BASE_RATE_N, list(range(MIN_BASE_RATE_N)))
    build_frequency_matrix("test_event", ["inflation"])
    rate = get_base_rate("test_event", "inflation")
    assert rate is not None
    assert rate == pytest.approx(1.0, abs=0.01)


# --- detect_parlay_edge tests ---

def test_parlay_edge_positive_when_correlated():
    # Create 10 events where A and B always appear together
    conn = sqlite3.connect(TEST_DB)
    for i in range(10):
        text = "Term alpha and term beta appear here together."
        conn.execute(
            "INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count) VALUES (?,?,?,?,?,?)",
            ("test", "speaker", "test_event", f"2024-01-{i+1:02d}", text, len(text.split()))
        )
    conn.commit()
    conn.close()

    terms = ["alpha", "beta"]
    build_frequency_matrix("test_event", terms)
    build_cooccurrence_matrix("test_event", terms)

    # Both have base_rate=1.0, P(B|A)=1.0, true_joint=1.0
    # Market assumes independence: 0.5 * 0.5 = 0.25 (but actually both are 1.0)
    # market_price_a=0.5, market_price_b=0.5, parlay_price=0.25
    result = detect_parlay_edge(
        "test_event",
        "alpha", 0.5,
        "beta", 0.5,
        parlay_market_price=0.25
    )
    assert result["true_joint_prob"] == pytest.approx(1.0)
    assert result["parlay_edge"] > 0
    assert result["trade_signal"] is True


def test_parlay_edge_no_signal_independent():
    # Create events where A and B appear independently
    conn = sqlite3.connect(TEST_DB)
    for i in range(10):
        # A appears in first 5, B appears in last 5 — no overlap
        if i < 5:
            text = "Term alpha appears in this transcript only."
        else:
            text = "Term beta appears in this transcript only."
        conn.execute(
            "INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count) VALUES (?,?,?,?,?,?)",
            ("test", "speaker", "test_event", f"2024-01-{i+1:02d}", text, len(text.split()))
        )
    conn.commit()
    conn.close()

    terms = ["alpha", "beta"]
    build_frequency_matrix("test_event", terms)
    build_cooccurrence_matrix("test_event", terms)

    # Both have base_rate=0.5, but they never co-occur → true_joint=0
    # Market assumes independence: 0.5 * 0.5 = 0.25
    result = detect_parlay_edge(
        "test_event",
        "alpha", 0.5,
        "beta", 0.5,
        parlay_market_price=0.25
    )
    assert result["true_joint_prob"] == pytest.approx(0.0, abs=0.01)
    assert result["parlay_edge"] < 0
    assert result["trade_signal"] is False
