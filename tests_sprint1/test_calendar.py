"""Test calendar/events.py — FOMC schedule is correct, prep window math works."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_calendar.events import (
    EVENTS, HomerunEvent, events_in_prep_window, get_event, next_event,
)


def test_fomc_events_present():
    """At least 6 FOMC pressers on calendar."""
    fomc = [e for e in EVENTS if e.event_type == "fomc_presser"]
    assert len(fomc) >= 6, f"expected ≥6 FOMC events, got {len(fomc)}"


def test_fomc_april_2026_is_real():
    """FOMC April 29 2026 is the first target (official Fed schedule).
    Kalshi markets close April 30 14:00 UTC but that's post-event settlement."""
    e = get_event("fomc_20260429")
    assert e is not None
    assert e.event_datetime_utc.year == 2026
    assert e.event_datetime_utc.month == 4
    assert e.event_datetime_utc.day == 29


def test_no_fomc_on_bogus_2026_05_07():
    """2026-05-07 was the OLD wrong date in config/events.py. Not in new calendar."""
    for e in EVENTS:
        if e.event_type == "fomc_presser":
            assert e.event_datetime_utc.strftime("%Y-%m-%d") != "2026-05-07"


def test_t_minus_math():
    """t_minus(72) should be exactly 72h before event."""
    e = EVENTS[0]
    t_minus = e.t_minus(72)
    delta = e.event_datetime_utc - t_minus
    assert delta == timedelta(hours=72)


def test_prep_window_true_at_60h_before():
    """Event 60h away should be in prep window."""
    ref = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    e = HomerunEvent(
        event_id="test",
        event_type="fomc_presser",
        event_datetime_utc=ref,
        title="Test",
        kalshi_series=["X"],
        corpus_event_type="fomc_presser",
    )
    now = ref - timedelta(hours=60)
    assert e.is_prep_window(now=now) is True


def test_prep_window_false_at_100h_before():
    """Event 100h away should NOT be in prep window."""
    ref = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    e = HomerunEvent(
        event_id="test",
        event_type="fomc_presser",
        event_datetime_utc=ref,
        title="Test",
        kalshi_series=["X"],
        corpus_event_type="fomc_presser",
    )
    now = ref - timedelta(hours=100)
    assert e.is_prep_window(now=now) is False


def test_next_event_returns_future():
    """next_event() should return something with date > now."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    e = next_event(now=now)
    assert e is not None
    assert e.event_datetime_utc > now


def test_next_event_returns_earliest():
    """next_event() returns the SOONEST future event."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    e = next_event(now=now)
    future = [x for x in EVENTS if x.event_datetime_utc > now]
    earliest = min(future, key=lambda x: x.event_datetime_utc)
    assert e.event_id == earliest.event_id


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
