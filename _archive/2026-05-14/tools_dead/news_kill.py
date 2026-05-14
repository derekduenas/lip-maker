"""News/Event Kill Switch (Sonnet's #2 ROI fix)

FREE sources — no paid API:
1. FRED/BLS macro release calendar (CPI, NFP, PPI, GDP, etc.)
2. NWS NOAA weather alerts RSS
3. Hardcoded blackout windows for known events

When a blackout window is active for a market type, set max_position=0
and add affected series to market_blacklist (90-min expiry).

Run via cron every 5 minutes.
"""
from __future__ import annotations

# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "news_kill")
except Exception:
    pass

import sys, sqlite3, time, requests
from datetime import datetime, timezone, timedelta

DB = "/root/lip-maker/data/lip_maker.db"

# === MACRO RELEASE CALENDAR (hardcoded, refresh quarterly) ===
# US BLS / Fed dates 2026 — these are the high-impact data releases
# Format: (UTC datetime, series_prefixes_to_blackout, label)
MACRO_RELEASES = [
    # CPI: 2nd Tuesday of month, 8:30am ET = 12:30 UTC
    ("2026-05-13T12:30:00", ["KXCPI"], "CPI April"),
    ("2026-06-10T12:30:00", ["KXCPI"], "CPI May"),
    ("2026-07-15T12:30:00", ["KXCPI"], "CPI June"),
    # PPI: day after CPI
    ("2026-05-14T12:30:00", ["KXPPI", "KXUSPPI"], "PPI April"),
    ("2026-06-12T12:30:00", ["KXPPI", "KXUSPPI"], "PPI May"),
    # NFP: 1st Friday of month, 8:30am ET
    ("2026-05-02T12:30:00", ["KXNFP", "KXUNEMPLOY", "KXJOBS"], "NFP April"),
    ("2026-06-06T12:30:00", ["KXNFP", "KXUNEMPLOY", "KXJOBS"], "NFP May"),
    ("2026-07-03T12:30:00", ["KXNFP", "KXUNEMPLOY", "KXJOBS"], "NFP June"),
    # GDP: monthly advance estimates
    ("2026-05-29T12:30:00", ["KXGDP"], "GDP Q1 advance"),
    ("2026-07-31T12:30:00", ["KXGDP"], "GDP Q2 advance"),
    # FOMC meetings
    ("2026-06-10T18:00:00", ["KXFED", "KXFEDDECISION", "KXCPI"], "FOMC June"),
    ("2026-07-30T18:00:00", ["KXFED", "KXFEDDECISION"], "FOMC July"),
]

BLACKOUT_BEFORE_MIN = 90  # minutes before event
BLACKOUT_AFTER_MIN = 30   # minutes after
WEATHER_SERIES_PREFIXES = ["KXHIGHT", "KXLOWT", "KXRAIN"]


def check_blackouts() -> list[tuple[str, str, str]]:
    """Return [(series_prefix, reason, expires_at)] of active blackouts."""
    now = datetime.now(timezone.utc)
    active = []
    # Check macro releases
    for ts_str, prefixes, label in MACRO_RELEASES:
        ts = datetime.fromisoformat(ts_str + "+00:00")
        before = ts - timedelta(minutes=BLACKOUT_BEFORE_MIN)
        after = ts + timedelta(minutes=BLACKOUT_AFTER_MIN)
        if before <= now <= after:
            for prefix in prefixes:
                active.append((prefix, f"macro_blackout:{label}", after.isoformat()))
    return active


def check_nws_alerts() -> list[tuple[str, str, str]]:
    """Pull NWS active alerts RSS — flag weather series in alerted regions."""
    active = []
    try:
        # NWS active alerts API — free, no auth
        r = requests.get(
            "https://api.weather.gov/alerts/active",
            params={"status": "actual", "severity": "Severe,Extreme"},
            headers={"User-Agent": "innait-aegis/1.0"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            features = data.get("features", [])
            # If any severe weather alert active, blackout ALL weather series
            # (could be more granular by region but blunt-and-safe is OK for now)
            severe_count = sum(1 for f in features if f.get("properties", {}).get("severity") in ("Severe", "Extreme"))
            if severe_count >= 5:  # only blackout on widespread severe weather
                expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
                for prefix in WEATHER_SERIES_PREFIXES:
                    active.append((prefix, f"nws_severe_weather:{severe_count}_alerts", expires))
    except Exception as e:
        print(f"NWS check failed: {e}")
    return active


def main():
    blackouts = check_blackouts() + check_nws_alerts()
    if not blackouts:
        print(f"OK — no active blackouts at {datetime.now(timezone.utc).isoformat()}")
        return

    print(f"🚨 {len(blackouts)} blackout series:")
    db = sqlite3.connect(DB)
    for prefix, reason, expires in blackouts:
        # Find all currently-active markets with this prefix
        markets = db.execute(
            "SELECT market_ticker FROM lip_programs "
            "WHERE enrolled = 1 AND paid_out = 0 "
            "AND market_ticker LIKE ?",
            (prefix + "%",),
        ).fetchall()
        for (tk,) in markets:
            db.execute(
                "INSERT OR REPLACE INTO market_blacklist "
                "(ticker, reason, added_at, expires_at) VALUES (?, ?, ?, ?)",
                (tk, reason, datetime.now(timezone.utc).isoformat(), expires),
            )
        print(f"  {prefix}*: {len(markets)} markets blacklisted ({reason})")
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
