"""Tests for engine/evolution.py"""

import sqlite3
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine.evolution import detect_emerging_terms, build_evolution_chart, _tokenize_bigrams

TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_evolution.db")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    monkeypatch.setattr("engine.evolution.DB_PATH", TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transcripts (id INTEGER PRIMARY KEY, source TEXT, speaker TEXT, event_type TEXT, event_date DATE, raw_text TEXT, word_count INTEGER);
        DELETE FROM transcripts;
    """)
    # Prior quarters: talk about FSD, autonomy — NOT "sovereign AI"
    for i, date in enumerate(["2024-01-15", "2024-04-15", "2024-07-15", "2024-10-15"]):
        conn.execute("INSERT INTO transcripts VALUES (?,?,?,?,?,?,?)",
                     (i+1, "earnings", "huang", "nvda_earnings", date,
                      "We see incredible demand for GPUs. Data center revenue is growing. Inference workloads are expanding. Training capacity is at maximum. " * 5,
                      50))
    # Recent quarters: introduce "sovereign AI" (new term)
    for i, date in enumerate(["2025-01-15", "2025-04-15"]):
        conn.execute("INSERT INTO transcripts VALUES (?,?,?,?,?,?,?)",
                     (i+10, "earnings", "huang", "nvda_earnings", date,
                      "Sovereign AI is the next frontier. Every country needs sovereign AI infrastructure. Sovereign AI spending is accelerating. Physical AI and agentic systems are emerging. " * 3,
                      40))
    conn.commit()
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_emerging_terms_finds_new_vocabulary():
    emerging = detect_emerging_terms("huang", "NVDA", recent_n=2, prior_n=4)
    terms = [e["term"] for e in emerging]
    assert "sovereign" in terms or "sovereign ai" in terms


def test_emerging_terms_filters_stopwords():
    emerging = detect_emerging_terms("huang", "NVDA", recent_n=2, prior_n=4)
    terms = [e["term"] for e in emerging]
    for t in terms:
        assert t not in {"the", "and", "is", "are", "was", "for", "with"}


def test_build_evolution_chart_returns_correct_quarters():
    monkeypatch_module = pytest.importorskip("engine.evolution")
    chart = build_evolution_chart("huang", "sovereign AI", "NVDA")
    assert len(chart) == 6  # 4 prior + 2 recent
    # First 4 should not mention sovereign AI
    assert chart[0]["mentioned"] is False
    # Last 2 should mention it
    assert chart[-1]["mentioned"] is True
