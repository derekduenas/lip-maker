"""Tests for prep/news_aggregator.py — dedup, recency, formatting."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.news_aggregator import (
    _dedupe_title, _parse_published, _recency_score, as_context_lines,
)


def test_dedupe_strips_outlet_suffix():
    """Common suffix patterns get stripped for dedup."""
    a = _dedupe_title("Powell says inflation cooling — CNBC")
    b = _dedupe_title("Powell says inflation cooling - Reuters")
    assert a == b, f"expected match, got {a!r} vs {b!r}"


def test_dedupe_case_insensitive():
    a = _dedupe_title("Fed Pause Expected")
    b = _dedupe_title("FED PAUSE EXPECTED")
    assert a == b


def test_parse_published_rfc2822():
    dt = _parse_published("Mon, 20 Apr 2026 10:15:00 GMT")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 4


def test_parse_published_iso8601():
    dt = _parse_published("2026-04-20T10:15:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.tzinfo is not None


def test_parse_published_bad_returns_none():
    assert _parse_published("") is None
    assert _parse_published("not-a-date") is None


def test_recency_now_is_full_score():
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    assert _recency_score(now, now) == 1.0


def test_recency_old_is_zero():
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    pub = now - timedelta(hours=100)
    assert _recency_score(pub, now) == 0.0


def test_recency_mid_window():
    """36h old → 0.5."""
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    pub = now - timedelta(hours=36)
    assert abs(_recency_score(pub, now) - 0.5) < 0.01


def test_recency_none_returns_half():
    """Missing pub_dt defaults to 0.5 (neutral)."""
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    assert _recency_score(None, now) == 0.5


def test_as_context_lines_formats():
    """Headlines render as bullet lines with source + date."""
    headlines = [
        {"title": "Powell tees up pause", "source": "reuters.com",
         "published": "Mon, 20 Apr 2026 10:15:00 GMT"},
        {"title": "CPI softer than expected"},
    ]
    lines = as_context_lines(headlines)
    assert len(lines) == 2
    assert "Powell tees up pause" in lines[0]
    assert "[reuters.com]" in lines[0]


def test_as_context_lines_respects_max_n():
    headlines = [{"title": f"Story {i}"} for i in range(30)]
    lines = as_context_lines(headlines, max_n=10)
    assert len(lines) == 10


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
