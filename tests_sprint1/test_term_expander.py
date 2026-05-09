"""Test prep/term_expander.py — title parsing, filtering, variants."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.term_expander import (
    filter_markets_for_event, parse_close_time, parse_title,
)


def test_parse_title_simple():
    """Standard title → extracts single word."""
    r = parse_title("Will Powell say Recession at his Apr 2026 press conference?")
    assert r is not None
    raw, variants = r
    assert raw == "Recession"
    assert variants == ["recession"]


def test_parse_title_slash_variants():
    """Slash-delimited → list of variants."""
    r = parse_title("Will Powell say QE / Quantitative Easing at his Apr 2026 press conference?")
    assert r is not None
    raw, variants = r
    assert raw == "QE / Quantitative Easing"
    assert variants == ["qe", "quantitative easing"]


def test_parse_title_three_variants():
    """Three variants."""
    r = parse_title(
        "Will Powell say Replace / Replaces / Replaced / Replacement "
        "at his Apr 2026 press conference?"
    )
    assert r is not None
    _, variants = r
    assert set(variants) == {"replace", "replaces", "replaced", "replacement"}


def test_parse_title_no_match():
    """Title that doesn't match pattern → None."""
    r = parse_title("Some unrelated market title about sports")
    assert r is None


def test_parse_close_time_z_format():
    """Kalshi ISO with Z suffix parses to UTC datetime."""
    dt = parse_close_time("2026-04-30T14:00:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 4 and dt.day == 30
    assert dt.hour == 14 and dt.tzinfo == timezone.utc


def test_parse_close_time_bad():
    """Invalid → None."""
    assert parse_close_time("") is None
    assert parse_close_time("not-a-date") is None


def test_filter_markets_within_window():
    """Markets with close_time within ±24h of event should be included."""
    event_dt = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    # Two markets: one matches event window, one doesn't
    raw = [
        {
            "ticker": "KXFEDMENTION-26APR-TEST1",
            "title": "Will Powell say Recession at his Apr 2026 press conference?",
            "close_time": "2026-04-30T14:00:00Z",
            "yes_bid": None, "yes_ask": None, "volume": None,
        },
        {
            "ticker": "KXFEDMENTION-26JUN-TEST2",
            "title": "Will Powell say Recession at his Jun 2026 press conference?",
            "close_time": "2026-06-10T14:00:00Z",
            "yes_bid": None, "yes_ask": None, "volume": None,
        },
    ]
    filtered = filter_markets_for_event(raw, event_dt, tolerance_hours=24)
    assert len(filtered) == 1
    assert filtered[0].ticker == "KXFEDMENTION-26APR-TEST1"
    assert filtered[0].word_variants == ["recession"]


def test_filter_rejects_non_mention_markets():
    """A non-Powell-say market should be dropped by title parser."""
    event_dt = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    raw = [
        {
            "ticker": "KXFED-27APR-T4.00",
            "title": "Will the upper bound of the federal funds rate be above 4.00%?",
            "close_time": "2026-04-30T14:00:00Z",
            "yes_bid": 25, "yes_ask": 30,
        },
    ]
    filtered = filter_markets_for_event(raw, event_dt)
    assert len(filtered) == 0


def test_filter_preserves_prices_and_volume():
    """Liquid markets — prices and volume carry through."""
    event_dt = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    raw = [
        {
            "ticker": "KXFEDMENTION-26APR-INFL",
            "title": "Will Powell say Inflation at his Apr 2026 press conference?",
            "close_time": "2026-04-30T14:00:00Z",
            "yes_bid": 72, "yes_ask": 78, "volume": 150, "liquidity": 2500,
        },
    ]
    filtered = filter_markets_for_event(raw, event_dt)
    assert len(filtered) == 1
    m = filtered[0]
    assert m.yes_bid == 72 and m.yes_ask == 78
    assert m.volume == 150 and m.liquidity == 2500


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
