"""Tests for config/watchlist.py"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEST_WATCHLIST = os.path.join(os.path.dirname(__file__), "..", "data", "test_watchlist.json")


@pytest.fixture(autouse=True)
def clean_watchlist(monkeypatch):
    monkeypatch.setattr("config.watchlist.WATCHLIST_PATH", TEST_WATCHLIST)
    if os.path.exists(TEST_WATCHLIST):
        os.remove(TEST_WATCHLIST)
    yield
    if os.path.exists(TEST_WATCHLIST):
        os.remove(TEST_WATCHLIST)


def test_add_to_watchlist_creates_entry():
    from config.watchlist import add_to_watchlist, _load_watchlist
    add_to_watchlist("pep_earnings", "PEP", "tariffs", 0.61, 6200)
    entries = _load_watchlist()
    assert len(entries) == 1
    assert entries[0]["event_type"] == "pep_earnings"
    assert entries[0]["term"] == "tariffs"
    assert entries[0]["seen_count"] == 1


def test_add_to_watchlist_increments_seen_count():
    from config.watchlist import add_to_watchlist, _load_watchlist
    add_to_watchlist("pep_earnings", "PEP", "tariffs", 0.61, 6200)
    add_to_watchlist("pep_earnings", "PEP", "tariffs", 0.55, 7000)
    entries = _load_watchlist()
    assert len(entries) == 1
    assert entries[0]["seen_count"] == 2
    assert entries[0]["market_price"] == 0.55  # updated to latest


def test_watchlist_report_runs_without_error(capsys):
    from config.watchlist import add_to_watchlist, watchlist_report
    add_to_watchlist("tsla_earnings", "TSLA", "autonomy", 0.76, 500)
    watchlist_report()
    captured = capsys.readouterr()
    assert "WATCHLIST" in captured.out
    assert "TSLA" in captured.out
