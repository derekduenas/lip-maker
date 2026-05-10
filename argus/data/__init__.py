"""Data adapters — venue-agnostic, cache-first.

Each adapter exposes a thin client over a free upstream:
    spotify_charts.py  — kworb/spotify scraper (Phase 2)
    nws_forecast.py    — National Weather Service (Sprint 2 brain)
    fda_calendar.py    — FDA approval calendar (Sprint 3 brain)
    polls.py           — election polling aggregates (Sprint 4 brain)

Conventions:
    1. Cache aggressively to data/cache/{adapter}/, indexed by query.
    2. Re-fetch only on cache miss or explicit refresh=True.
    3. Return typed dataclasses, never raw dicts.
    4. Raise on hard failure; return None / empty on missing data.
"""
