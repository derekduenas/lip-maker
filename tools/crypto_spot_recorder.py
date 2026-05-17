"""Crypto spot price recorder for hedger basis calc (Phase E, 2026-05-16).

Fetches live spot prices for the crypto series in HEDGE_MAP from CoinGecko
(free, no auth), writes one row per Kalshi prefix into futures_prices.

Run on a 60-second systemd timer. cross_venue.hedger._latest_spot() reads
the most-recent row by prefix.

Why CoinGecko (not Kraken):
  - HYPE (Hyperliquid token) is not on Kraken; CoinGecko has it.
  - One free endpoint covers all 8 tokens in a single request.
  - We're only using this for SPOT REFERENCE — execution still routes
    through the venue-specific adapter (Kraken/Hyperliquid/etc.).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import urllib.request
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)

# CoinGecko ID  →  Kalshi series prefixes that need this spot
# (each prefix may have its own derived strike-binary structure)
COINGECKO_TO_PREFIXES = {
    "bitcoin":     ["KXBTCMINMON", "KXBTCMAXMON"],
    "ethereum":    ["KXETHMINMON", "KXETHMAXMON"],
    "ripple":      ["KXXRPMINMON", "KXXRPMAXMON"],
    "solana":      ["KXSOLMINMON", "KXSOLMAXMON"],
    "cardano":     ["KXADAMINMON", "KXADAMAXMON"],
    "dogecoin":    ["KXDOGEMINMON", "KXDOGEMAXMON"],
    "zcash":       ["KXZECMINMON", "KXZECMAXMON"],
    "hyperliquid": ["KXHYPEMINMON", "KXHYPEMAXMON"],
}

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price?"
    "ids={ids}&vs_currencies=usd"
)
TIMEOUT_SEC = 10


def fetch_spots() -> dict:
    ids = ",".join(COINGECKO_TO_PREFIXES.keys())
    url = COINGECKO_URL.format(ids=ids)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "innait-lip-maker/1.0"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return json.loads(r.read().decode())


def write_to_db(spots: dict, db_path: str = settings.DB_PATH) -> int:
    if not spots:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=10.0)
    written = 0
    try:
        for gecko_id, info in spots.items():
            price = float(info.get("usd") or 0)
            if price <= 0:
                continue
            for prefix in COINGECKO_TO_PREFIXES.get(gecko_id, []):
                conn.execute(
                    """INSERT INTO futures_prices
                       (kalshi_prefix, yahoo_symbol, name, price, fetched_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (prefix, f"{gecko_id}-USD", f"{gecko_id} (coingecko)",
                     price, now_iso),
                )
                written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        spots = fetch_spots()
    except Exception as e:
        _log.error(f"coingecko fetch failed: {e}")
        return 1
    n = write_to_db(spots)
    quoted = ", ".join(
        f"{g}=${info.get('usd', '?')}"
        for g, info in spots.items()
    )
    _log.info(f"recorded {n} rows  ({quoted})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
