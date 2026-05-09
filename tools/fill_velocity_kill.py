"""Fill velocity kill switch — adverse selection protection.

Polls Kalshi for fills, counts per-market in rolling 30s window.
If 3+ fills on one market in 30s → write to market_blacklist (30-min expiry).
Quote_manager already respects blacklist, so quotes will be cancelled.

Run via cron every 30 seconds.
"""
from __future__ import annotations

# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "fill_velocity_kill")
except Exception:
    pass

import sys, time, sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/root/lip-maker")

from execution.kalshi_auth import KalshiClient

THRESHOLD_FILLS = 3
WINDOW_SECONDS = 30
BLACKLIST_HOURS = 0.5

c = KalshiClient()

# Get fills in last 60s (a bit larger window for safety)
min_ts = int(time.time()) - 60
try:
    r = c.get("/portfolio/fills", params={"limit": 200, "min_ts": min_ts})
except Exception as e:
    print(f"FILLS API ERROR: {e}")
    sys.exit(0)
fills = r.get("fills", [])

# Bucket by market in 30s rolling window
now = time.time()
fill_count = defaultdict(int)
for f in fills:
    ts_str = f.get("created_time", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        age = now - ts.timestamp()
    except Exception:
        continue
    if age <= WINDOW_SECONDS:
        tk = f.get("ticker", "?")
        fill_count[tk] += 1

# Find markets with too-many fills
toxic = {tk: n for tk, n in fill_count.items() if n >= THRESHOLD_FILLS}

if not toxic:
    print(f"OK — no fill velocity alerts (checked {len(fills)} fills in last 60s)")
    sys.exit(0)

# Add to blacklist
db = sqlite3.connect("/root/lip-maker/data/lip_maker.db")
expires = (datetime.now(timezone.utc) + timedelta(hours=BLACKLIST_HOURS)).isoformat()
for tk, n in toxic.items():
    db.execute(
        "INSERT OR REPLACE INTO market_blacklist (ticker, reason, added_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (tk, f"fill_velocity:{n}_fills_in_{WINDOW_SECONDS}s",
         datetime.now(timezone.utc).isoformat(), expires)
    )
    print(f"🚨 BLACKLISTED {tk}: {n} fills in {WINDOW_SECONDS}s (expires {expires[:16]})")
db.commit()
db.close()

