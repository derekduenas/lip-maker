"""Macro event calendar for LIP quote-loop blackouts.

Each event defines:
  - name: human-readable
  - event_utc: scheduled datetime
  - pre_min: minutes BEFORE event to start blackout (quotes cancelled)
  - post_min: minutes AFTER event before re-entering
  - ticker_prefixes: market-ticker prefixes this event affects

The macro_blackout_sync tool injects expires_at rows into market_blacklist,
so the existing lip_discovery filter transparently skips affected markets.

Recurring events (EIA weekly, NFP monthly) expand over a 60-day window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List


@dataclass
class MacroEvent:
    name: str
    event_utc: datetime
    pre_min: int
    post_min: int
    ticker_prefixes: List[str] = field(default_factory=list)


# ── Ticker-prefix buckets ─────────────────────────────────────────────
PETROLEUM   = ["KXBRENTD", "KXHOILW", "KXCRUDE"]
NATGAS      = ["KXNATGASD"]
GRAIN       = ["KXCORNW", "KXWHEATW", "KXSOYBEANW"]
SOFTS       = ["KXCOCOAW", "KXCOFFEEW", "KXSUGARW"]
METALS      = ["KXGOLDD", "KXSILVERD", "KXCOPPERD"]
CRYPTO      = ["KXBTC", "KXETH", "KXSOL"]
MACRO_BROAD = PETROLEUM + NATGAS + GRAIN + SOFTS + METALS + CRYPTO


def _next_weekday(weekday: int, hour: int, minute: int,
                  from_date: datetime | None = None) -> datetime:
    """Next occurrence of weekday (Mon=0..Sun=6) at hour:minute UTC."""
    from_date = from_date or datetime.now(timezone.utc)
    days_ahead = (weekday - from_date.weekday()) % 7
    target = from_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target += timedelta(days=days_ahead)
    if target <= from_date:
        target += timedelta(days=7)
    return target


def _expand_weekly(weekday: int, hour: int, minute: int,
                   weeks: int = 8) -> List[datetime]:
    out = []
    t = _next_weekday(weekday, hour, minute)
    for _ in range(weeks):
        out.append(t)
        t = t + timedelta(days=7)
    return out


def upcoming_events(lookhead_days: int = 60) -> List[MacroEvent]:
    """Generate scheduled macro events over the next `lookhead_days` window.

    ET → UTC conversion: treating April/Oct DST (UTC-4 during summer, UTC-5 winter).
    We're currently in EDT (UTC-4): 8:30 ET = 12:30 UTC, 10:30 ET = 14:30 UTC,
    12:00 ET = 16:00 UTC, 14:00 ET = 18:00 UTC.
    """
    events: List[MacroEvent] = []
    cutoff = datetime.now(timezone.utc) + timedelta(days=lookhead_days)

    # ── EIA petroleum: Wed 10:30 ET = 14:30 UTC ──
    # Releases weekly crude oil stocks. Moves BRENT, HOIL.
    for dt in _expand_weekly(weekday=2, hour=14, minute=30, weeks=9):
        if dt > cutoff: break
        events.append(MacroEvent(
            name="EIA_petroleum",
            event_utc=dt,
            pre_min=30,
            post_min=60,
            ticker_prefixes=PETROLEUM,
        ))

    # ── EIA natural gas: Thu 10:30 ET = 14:30 UTC ──
    for dt in _expand_weekly(weekday=3, hour=14, minute=30, weeks=9):
        if dt > cutoff: break
        events.append(MacroEvent(
            name="EIA_natgas",
            event_utc=dt,
            pre_min=30,
            post_min=60,
            ticker_prefixes=NATGAS,
        ))

    # ── USDA WASDE: ~9th of each month 12:00 ET = 16:00 UTC ──
    # Actual dates released yearly — hardcode next 2 known releases
    # Historical pattern: 9th-12th of each month
    wasde_dates = [
        datetime(2026, 5, 12, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc),
    ]
    for dt in wasde_dates:
        if dt > cutoff: break
        events.append(MacroEvent(
            name="USDA_WASDE",
            event_utc=dt,
            pre_min=45,
            post_min=90,
            ticker_prefixes=GRAIN + SOFTS,
        ))

    # ── FOMC 2026 schedule (verified against federalreserve.gov + Sovereign calendar) ──
    # Per-meeting: 2:00 PM ET statement, 2:30 PM ET press conf → use 2:00 ET = 18:00 UTC
    # Sovereign's canonical times: Apr 29 / Jun 10 / Jul 29 / Sep 16 / Oct 28 / Dec 9 (all 18:30 UTC = 2:30 ET presser).
    # LIP uses 18:00 UTC to start blackout 30 min earlier (catches the statement release).
    fomc_dates = [
        datetime(2026, 4, 29, 18, 0, tzinfo=timezone.utc),  # 2:00 ET Apr 29
        datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc),  # 2:00 ET Jun 10 (was incorrectly Jun 17)
    ]
    for dt in fomc_dates:
        if dt > cutoff: break
        events.append(MacroEvent(
            name="FOMC",
            event_utc=dt,
            pre_min=60,
            post_min=180,  # long — press conf runs 2h
            ticker_prefixes=MACRO_BROAD,
        ))

    # ── NFP: first Friday 8:30 ET = 12:30 UTC ──
    # Next: May 1, Jun 5 (est)
    nfp_dates = [
        datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc),
        datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc),
    ]
    for dt in nfp_dates:
        if dt > cutoff: break
        events.append(MacroEvent(
            name="NFP",
            event_utc=dt,
            pre_min=30,
            post_min=60,
            ticker_prefixes=MACRO_BROAD,
        ))

    # ── CPI: ~10th of each month 8:30 ET = 12:30 UTC ──
    cpi_dates = [
        datetime(2026, 5, 13, 12, 30, tzinfo=timezone.utc),
        datetime(2026, 6, 11, 12, 30, tzinfo=timezone.utc),
    ]
    for dt in cpi_dates:
        if dt > cutoff: break
        events.append(MacroEvent(
            name="CPI",
            event_utc=dt,
            pre_min=30,
            post_min=60,
            ticker_prefixes=MACRO_BROAD,
        ))

    return sorted(events, key=lambda e: e.event_utc)


def active_blackouts(now: datetime | None = None) -> List[dict]:
    """Return events whose blackout window is currently active."""
    now = now or datetime.now(timezone.utc)
    active = []
    for ev in upcoming_events(lookhead_days=7):
        start = ev.event_utc - timedelta(minutes=ev.pre_min)
        end   = ev.event_utc + timedelta(minutes=ev.post_min)
        if start <= now <= end:
            active.append({
                "name": ev.name,
                "event_utc": ev.event_utc.isoformat(),
                "blackout_start": start.isoformat(),
                "blackout_end":   end.isoformat(),
                "ticker_prefixes": ev.ticker_prefixes,
            })
    return active


def upcoming_blackouts(hours_ahead: int = 48) -> List[dict]:
    """Upcoming events whose blackout hasn't started yet (for dashboard)."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    upcoming = []
    for ev in upcoming_events(lookhead_days=hours_ahead // 24 + 1):
        start = ev.event_utc - timedelta(minutes=ev.pre_min)
        if now < start <= cutoff:
            upcoming.append({
                "name": ev.name,
                "event_utc": ev.event_utc.isoformat(),
                "blackout_starts_in_min": int((start - now).total_seconds() / 60),
                "ticker_prefixes": ev.ticker_prefixes,
            })
    return sorted(upcoming, key=lambda e: e["event_utc"])
