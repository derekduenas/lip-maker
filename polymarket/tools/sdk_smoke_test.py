"""SDK smoke test — verify polymarket-us SDK works against user's keys.

Tests:
  1. Account balances (the endpoint we couldn't find before)
  2. List markets (with rewards filter if available)
  3. List positions (should be 0 for fresh deposit)
  4. orders.preview (server-side paper validation — won't actually place)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env
from execution.pm_auth import _load_dotenv_simple
_load_dotenv_simple(str(Path(__file__).resolve().parent.parent / ".env"))

from polymarket_us import PolymarketUS

key_id = os.getenv("PM_API_KEY_ID")
secret_path = Path(__file__).resolve().parent.parent / "config" / "polymarket_secret_key.b64"
secret_key = secret_path.read_text().strip()

print(f"  key_id: {key_id[:8]}...")
print(f"  secret loaded ({len(secret_key)} chars)")

client = PolymarketUS(key_id=key_id, secret_key=secret_key)

print("\n--- 1. Account balances ---")
try:
    bal = client.account.balances()
    print(f"  ✓ {bal}")
except Exception as e:
    print(f"  🔴 {type(e).__name__}: {str(e)[:200]}")

print("\n--- 2. Markets list (limit 3) ---")
try:
    markets = client.markets.list({"limit": 3})
    print(f"  ✓ got {len(markets) if hasattr(markets,'__len__') else type(markets).__name__}")
    if hasattr(markets, '__iter__'):
        for m in list(markets)[:2]:
            slug = getattr(m, 'slug', None) or (m.get('slug') if isinstance(m, dict) else '?')
            print(f"    - {slug}")
except Exception as e:
    print(f"  🔴 {type(e).__name__}: {str(e)[:200]}")

print("\n--- 3. Portfolio positions ---")
try:
    pos = client.portfolio.positions()
    print(f"  ✓ {pos}")
except Exception as e:
    print(f"  🔴 {type(e).__name__}: {str(e)[:200]}")

print("\n--- 4. Orders preview (server-side paper) ---")
try:
    preview = client.orders.preview({
        "marketSlug": "btc-100k-2025",
        "intent": "ORDER_INTENT_BUY_LONG",
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": "0.55", "currency": "USD"},
        "quantity": 100,
    })
    print(f"  ✓ {preview}")
except Exception as e:
    print(f"  🔴 {type(e).__name__}: {str(e)[:300]}")
