"""Find active rewards-eligible markets on Polymarket US (not gamma global).

Probes the SDK markets.list with various filters to find what's actually
quotable on the user's polymarket.us account.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS

key_id = os.getenv("PM_API_KEY_ID")
secret_path = PROJECT_ROOT / "config" / "polymarket_secret_key.b64"
client = PolymarketUS(key_id=key_id, secret_key=secret_path.read_text().strip())

print("--- Probe 1: bigger limit, see total inventory ---")
try:
    r = client.markets.list({"limit": 50})
    markets = r.get("markets", []) if isinstance(r, dict) else r
    print(f"  total returned: {len(markets)}")
    if markets:
        # Show all unique categories
        cats = set()
        for m in markets:
            c = m.get("category") if isinstance(m, dict) else getattr(m, 'category', None)
            cats.add(c)
        print(f"  categories: {cats}")
        # Sample one with all fields
        print(f"\n  --- first market all fields ---")
        m = markets[0]
        print(json.dumps(m, indent=2, default=str)[:1500])
except Exception as e:
    print(f"  🔴 {type(e).__name__}: {str(e)[:200]}")

print("\n--- Probe 2: filter active+open ---")
for filter_set in [
    {"limit": 5, "active": True},
    {"limit": 5, "closed": False},
    {"limit": 5, "active": True, "closed": False},
    {"limit": 5, "category": "sports"},
    {"limit": 5, "category": "politics"},
]:
    try:
        r = client.markets.list(filter_set)
        n = len(r.get("markets", []) if isinstance(r, dict) else r)
        print(f"  {filter_set}: {n} markets")
    except Exception as e:
        print(f"  🔴 {filter_set}: {type(e).__name__}: {str(e)[:100]}")

print("\n--- Probe 3: search for specific slugs from gamma-api ---")
test_slugs = [
    "btc-100k-2025",
    "will-bitcoin-hit-150k-by-december-31-2026",
    "san-antonio-spurs-nba-2026",
]
for s in test_slugs:
    try:
        r = client.markets.retrieveBySlug(s) if hasattr(client.markets, 'retrieveBySlug') else None
        print(f"  ✓ {s}: {r}")
    except Exception as e:
        print(f"  · {s}: {type(e).__name__}: {str(e)[:100]}")

print("\n--- Probe 4: events list (Polymarket's hierarchy) ---")
try:
    r = client.events.list({"limit": 10})
    events = r.get("events", []) if isinstance(r, dict) else r
    print(f"  got {len(events)} events")
    for e in events[:5]:
        slug = e.get("slug") if isinstance(e, dict) else '?'
        title = e.get("title") if isinstance(e, dict) else '?'
        print(f"  · {slug}: {title}")
except Exception as e:
    print(f"  🔴 {type(e).__name__}: {str(e)[:200]}")
