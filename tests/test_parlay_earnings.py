"""Tests for engine/parlay_earnings.py"""

import sqlite3
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine.parlay_earnings import scan_earnings_parlays, narrative_block_detector

TEST_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_parlay_e.db")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    monkeypatch.setattr("engine.parlay_earnings.DB_PATH", TEST_DB)
    monkeypatch.setattr("engine.frequency.DB_PATH", TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS frequency_matrix (id INTEGER PRIMARY KEY, speaker TEXT, event_type TEXT, term TEXT, occurrences INTEGER, total_events INTEGER, base_rate REAL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, avg_count_per_mention REAL DEFAULT 0, max_count INTEGER DEFAULT 0, UNIQUE(speaker, event_type, term));
        CREATE TABLE IF NOT EXISTS cooccurrence (id INTEGER PRIMARY KEY, event_type TEXT, term_a TEXT, term_b TEXT, both_mentioned INTEGER, a_mentioned INTEGER, p_b_given_a REAL, UNIQUE(event_type, term_a, term_b));
        DELETE FROM frequency_matrix; DELETE FROM cooccurrence;
    """)
    # Insert test frequency data: alpha and beta both at 50%, but highly correlated
    conn.execute("INSERT INTO frequency_matrix (speaker,event_type,term,occurrences,total_events,base_rate) VALUES ('test','test_earnings','alpha',5,10,0.50)")
    conn.execute("INSERT INTO frequency_matrix (speaker,event_type,term,occurrences,total_events,base_rate) VALUES ('test','test_earnings','beta',5,10,0.50)")
    # Co-occurrence: P(beta|alpha) = 0.80 (strong correlation)
    conn.execute("INSERT INTO cooccurrence (event_type,term_a,term_b,both_mentioned,a_mentioned,p_b_given_a) VALUES ('test_earnings','alpha','beta',4,5,0.80)")
    conn.commit()
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_earnings_parlay_edge_positive_when_correlated():
    results = scan_earnings_parlays("test")
    assert len(results) > 0
    # true_joint = 0.50 * 0.80 = 0.40, indep = 0.50 * 0.50 = 0.25
    assert results[0]["true_joint"] == pytest.approx(0.40, abs=0.01)
    assert results[0]["edge_pp"] > 10  # 15pp edge


def test_narrative_block_detector():
    # Add strong co-occurrence pairs
    conn = sqlite3.connect(TEST_DB)
    conn.execute("INSERT INTO cooccurrence (event_type,term_a,term_b,both_mentioned,a_mentioned,p_b_given_a) VALUES ('test_earnings','alpha','gamma',5,5,1.00)")
    conn.execute("INSERT INTO cooccurrence (event_type,term_a,term_b,both_mentioned,a_mentioned,p_b_given_a) VALUES ('test_earnings','gamma','alpha',5,5,1.00)")
    conn.commit()
    conn.close()

    blocks = narrative_block_detector("test", "test")
    assert len(blocks) > 0
    # alpha and gamma should be in same block
    found = False
    for block in blocks:
        if "alpha" in block and "gamma" in block:
            found = True
    assert found
