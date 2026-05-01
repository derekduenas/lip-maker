"""Polymarket fair-value gate — adverse selection defense.

For markets where we have an external truth signal, refuse to quote
when the orderbook is materially mispriced vs that truth (someone
informed is about to take us out).

KNOWN SIGNALS:
  - Crypto (BTC/ETH/SOL): pull spot from Coinbase /v2/prices/{pair}/spot
    (free, no auth, ~200ms). Compare to market's strike + condition.
  - Weather: skip — no easy free real-time temp API
  - Sports: skip — would need ESPN/SportsRadar paid feed
  - Politics/macro: skip — no public truth

LOGIC:
  fair_value_skip(slug, yes_bid, yes_ask) → reason or None
  - If slug not in supported categories → return None (no opinion, allow)
  - If we can't fetch fair → return None (don't block on infra issue)
  - If fair clearly disagrees with current price → return skip reason

CACHE:
  60s TTL on fair-value lookups to avoid hammering external APIs.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request

_log = logging.getLogger(__name__)

CACHE_TTL_SEC = 60
_cache: dict[str, tuple[float, float]] = {}  # symbol → (price, fetched_ts)


def _http_get_json(url: str, timeout: int = 5) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pm-maker/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        _log.debug(f"http_get failed {url}: {e}")
        return None


def get_crypto_spot(symbol: str) -> float | None:
    """Pull spot from Coinbase. symbol = 'BTC' / 'ETH' / 'SOL'."""
    now = time.time()
    cached = _cache.get(symbol)
    if cached and now - cached[1] < CACHE_TTL_SEC:
        return cached[0]
    url = f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot"
    r = _http_get_json(url)
    if not r:
        return None
    try:
        price = float(r.get("data", {}).get("amount"))
        _cache[symbol] = (price, now)
        return price
    except (TypeError, ValueError):
        return None


def parse_crypto_market(slug: str) -> tuple[str, float, str] | None:
    """Parse crypto Polymarket slug.
    Returns (symbol, strike, direction) or None.

    Examples:
      will-bitcoin-hit-150k-by-december-31-2026 → ('BTC', 150000, 'above')
      will-ethereum-hit-5k-by-june-30-2026 → ('ETH', 5000, 'above')
    """
    s = slug.lower()
    # Symbol detection
    if "bitcoin" in s or "btc" in s:
        symbol = "BTC"
    elif "ethereum" in s or "eth" in s:
        symbol = "ETH"
    elif "solana" in s or "sol" in s:
        symbol = "SOL"
    else:
        return None

    # Strike — look for patterns like "150k", "5k", "100k"
    m = re.search(r"(\d+)k", s)
    if not m:
        return None
    strike_k = int(m.group(1))
    strike = strike_k * 1000

    # Direction — "hit" implies "reach or exceed"
    direction = "above" if "hit" in s else "above"  # default

    return (symbol, strike, direction)


def fair_value_skip(slug: str, yes_bid: float, yes_ask: float) -> str | None:
    """Return reason to skip if market is fair-value-mispriced.
    Returns None to allow quoting.

    Signals:
      - Crypto markets: compare spot price to strike
        - If spot >> strike (settles YES with high confidence): skip if no_bid > 30c
        - If spot << strike (settles NO): skip if yes_bid > 30c
    """
    parsed = parse_crypto_market(slug)
    if parsed:
        symbol, strike, direction = parsed
        spot = get_crypto_spot(symbol)
        if spot is None:
            return None  # infra issue, don't block

        # How far is spot from strike (as fraction of strike)?
        delta_pct = (spot - strike) / strike

        # If spot is well above strike (settles YES), no_bid should be near 0
        if delta_pct > 0.05:  # spot > 5% above strike
            no_bid_implied = 1.0 - yes_ask
            if no_bid_implied > 0.30:
                return (f"crypto_fv_skip[{symbol}] spot=${spot:.0f} >> strike=${strike} "
                        f"(Δ={delta_pct:+.1%}) → settles YES; NO bid {no_bid_implied:.2f}c too high")
        # If spot well below strike (settles NO), yes_bid should be near 0
        if delta_pct < -0.05:
            if yes_bid > 0.30:
                return (f"crypto_fv_skip[{symbol}] spot=${spot:.0f} << strike=${strike} "
                        f"(Δ={delta_pct:+.1%}) → settles NO; YES bid {yes_bid:.2f} too high")

    return None  # no opinion or no problem
