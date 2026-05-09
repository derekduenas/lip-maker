"""Homerun event calendar — canonical schedule of targets.

Each entry is a discrete, scheduled event where we'll prep deeply and bet
concentrated. Sources of truth:
  - FOMC dates: federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Kalshi markets: verified by querying /markets?series_ticker=KXFEDMENTION

Reviewed 2026-04-20 — the OLD config/events.py listed 2026-05-07 as next
FOMC which is NOT a real Fed meeting date. This file corrects that using
authoritative Kalshi close_time data.

Each event has a "t_minus_hours" trigger schedule:
  T-72h: corpus compile begins
  T-48h: term list built + base rates computed
  T-24h: thesis document generated + user review
  T-2h:  positions placed
  T+0:   live monitor (press conference)
  T+24h: resolution + review
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass
class HomerunEvent:
    """A scheduled event where we target concentrated bets."""
    event_id:            str        # unique string ID, e.g., "fomc_20260430"
    event_type:          str        # fomc_presser | scotus_opinion | cpi | nfp | testimony
    event_datetime_utc:  datetime   # when the event actually happens
    title:               str        # human-readable
    kalshi_series:       list[str]  # which Kalshi series we'll quote from
    corpus_event_type:   str        # key into sovereign DB transcripts table
    min_corpus_size:     int = 10   # minimum transcripts needed for confidence
    max_position_pct:    float = 0.30   # max 30% bankroll on any single event
    max_per_term_pct:    float = 0.08   # max 8% bankroll per single term
    notes:               str = ""

    def t_minus(self, hours: float) -> datetime:
        """Return the UTC timestamp `hours` before event."""
        return self.event_datetime_utc - timedelta(hours=hours)

    def is_prep_window(self, now: Optional[datetime] = None) -> bool:
        """Are we within the 72h prep window?"""
        now = now or datetime.now(timezone.utc)
        return self.t_minus(72) <= now < self.event_datetime_utc


# ═════════════════════════════════════════════════════════════════
# 2026 CONFIRMED EVENT CALENDAR
# ═════════════════════════════════════════════════════════════════

EVENTS: list[HomerunEvent] = [
    # ── FOMC Press Conferences ──────────────────────────────────────
    # Official Fed schedule: two-day meetings, press conf on day 2.
    # April 2026 meeting: 28-29. Press conf April 29 at 2:30pm ET.
    # Kalshi market close 2026-04-30 14:00 UTC is next-morning settlement,
    # NOT the event time itself. Corrected 2026-04-20 via news-feed cross-check.
    HomerunEvent(
        event_id="fomc_20260429",
        event_type="fomc_presser",
        event_datetime_utc=datetime(2026, 4, 29, 18, 30, tzinfo=timezone.utc),  # 2:30pm ET
        title="FOMC April 28-29, 2026 Press Conference",
        kalshi_series=["KXFEDMENTION"],
        corpus_event_type="fomc_presser",
        min_corpus_size=20,
        notes="First homerun target. 48 mention markets live, all illiquid.",
    ),
    HomerunEvent(
        event_id="fomc_20260610",
        event_type="fomc_presser",
        event_datetime_utc=datetime(2026, 6, 10, 18, 30, tzinfo=timezone.utc),
        title="FOMC June 9-10, 2026 Press Conference (with SEP)",
        kalshi_series=["KXFEDMENTION"],
        corpus_event_type="fomc_presser",
        min_corpus_size=20,
        notes="SEP meeting — dot plot release. Higher volatility expected.",
    ),
    HomerunEvent(
        event_id="fomc_20260729",
        event_type="fomc_presser",
        event_datetime_utc=datetime(2026, 7, 29, 18, 30, tzinfo=timezone.utc),
        title="FOMC July 28-29, 2026 Press Conference",
        kalshi_series=["KXFEDMENTION"],
        corpus_event_type="fomc_presser",
        min_corpus_size=20,
    ),
    HomerunEvent(
        event_id="fomc_20260916",
        event_type="fomc_presser",
        event_datetime_utc=datetime(2026, 9, 16, 18, 30, tzinfo=timezone.utc),
        title="FOMC September 15-16, 2026 Press Conference (with SEP)",
        kalshi_series=["KXFEDMENTION"],
        corpus_event_type="fomc_presser",
        min_corpus_size=20,
        notes="SEP meeting.",
    ),
    HomerunEvent(
        event_id="fomc_20261028",
        event_type="fomc_presser",
        event_datetime_utc=datetime(2026, 10, 28, 18, 30, tzinfo=timezone.utc),
        title="FOMC October 27-28, 2026 Press Conference",
        kalshi_series=["KXFEDMENTION"],
        corpus_event_type="fomc_presser",
        min_corpus_size=20,
    ),
    HomerunEvent(
        event_id="fomc_20261209",
        event_type="fomc_presser",
        event_datetime_utc=datetime(2026, 12, 9, 18, 30, tzinfo=timezone.utc),
        title="FOMC December 8-9, 2026 Press Conference (with SEP)",
        kalshi_series=["KXFEDMENTION"],
        corpus_event_type="fomc_presser",
        min_corpus_size=20,
        notes="Last SEP of the year.",
    ),
]


def next_event(now: Optional[datetime] = None) -> Optional[HomerunEvent]:
    """Return the next upcoming event."""
    now = now or datetime.now(timezone.utc)
    future = [e for e in EVENTS if e.event_datetime_utc > now]
    return min(future, key=lambda e: e.event_datetime_utc) if future else None


def events_in_prep_window(now: Optional[datetime] = None) -> list[HomerunEvent]:
    """Events currently in the 72h prep window."""
    now = now or datetime.now(timezone.utc)
    return [e for e in EVENTS if e.is_prep_window(now)]


def get_event(event_id: str) -> Optional[HomerunEvent]:
    for e in EVENTS:
        if e.event_id == event_id:
            return e
    return None
