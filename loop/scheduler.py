"""Scheduler — cron-style trigger before known events (FOMC, earnings).

This module wraps config/events.py with scheduling logic. The monitor loop
calls get_events_needing_scan() directly, but this module can be used for
standalone cron-based triggering outside the main loop.

Usage:
    python loop/scheduler.py --check     # print events needing scan now
    python loop/scheduler.py --next      # print next upcoming event
"""

import argparse
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.events import UPCOMING_EVENTS, get_events_needing_scan, get_upcoming_events


def next_event() -> dict | None:
    """Returns the single next upcoming event."""
    now = datetime.utcnow()
    soonest = None
    soonest_dt = None
    for event in UPCOMING_EVENTS:
        event_dt = datetime.strptime(event["event_date"], "%Y-%m-%d")
        if event_dt > now:
            if soonest_dt is None or event_dt < soonest_dt:
                soonest = event
                soonest_dt = event_dt
    return soonest


def hours_until_next_scan() -> float:
    """Returns hours until the next event needs scanning."""
    now = datetime.utcnow()
    for event in sorted(UPCOMING_EVENTS, key=lambda e: e["scan_at"]):
        scan_dt = datetime.strptime(event["scan_at"], "%Y-%m-%dT%H:%M:%S")
        if scan_dt > now:
            return (scan_dt - now).total_seconds() / 3600
    return float("inf")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Scheduler")
    parser.add_argument("--check", action="store_true", help="Show events needing scan now")
    parser.add_argument("--next", action="store_true", help="Show next upcoming event")
    args = parser.parse_args()

    if args.check:
        events = get_events_needing_scan(within_hours=24)
        if events:
            print(f"{len(events)} events need scanning:")
            for e in events:
                print(f"  {e['event_type']} on {e['event_date']} (scan_at {e['scan_at']})")
        else:
            hrs = hours_until_next_scan()
            print(f"No events need scanning now. Next scan in {hrs:.1f}h.")
    elif args.next:
        nxt = next_event()
        if nxt:
            print(f"Next event: {nxt['event_type']} on {nxt['event_date']} (speaker: {nxt.get('speaker','')})")
            hrs = hours_until_next_scan()
            print(f"Scan fires in {hrs:.1f}h")
        else:
            print("No upcoming events.")
    else:
        # Default: show status
        nxt = next_event()
        if nxt:
            print(f"Next: {nxt['event_type']} {nxt['event_date']} | Scan in {hours_until_next_scan():.1f}h")
        events_7d = get_events_needing_scan(within_hours=168)
        print(f"Events within 7 days: {len(events_7d)}")
        for e in events_7d:
            print(f"  {e['event_type']:<20} {e['event_date']}  scan_at={e['scan_at']}")
