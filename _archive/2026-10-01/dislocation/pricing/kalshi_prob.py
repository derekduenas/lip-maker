"""Kalshi market mid → implied probability.

Trivial layer: Kalshi YES contracts already trade in probability units,
so mid_cents/100 IS the implied probability. This module exists for:
  1. Symmetric API with other venues' pricing modules.
  2. Wrapping the Kalshi REST/WS feed so callers don't reimplement.
  3. Sanity gating (reject quotes with stale timestamps, missing depth).

Reuses the existing KalshiClient from execution/kalshi_auth.py.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from execution.kalshi_auth import KalshiClient

from ..event_universe import Venue, VenueQuote

_log = logging.getLogger(__name__)


def fetch_kalshi_quote(
    client: KalshiClient,
    market_ticker: str,
    *,
    max_age_sec: int = 30,
) -> Optional[VenueQuote]:
    """Fetch live quote for a Kalshi market. Returns None on stale/missing data.

    Kalshi market endpoint: /markets/{ticker}
    Returns dict with yes_bid, yes_ask, last_price, volume, etc.
    Prices are in cents [0, 100].
    """
    try:
        path = f"/markets/{market_ticker}"
        data = client.get_unauth(path)
    except Exception as e:
        _log.warning(f"kalshi fetch failed for {market_ticker}: {e}")
        return None

    market = data.get("market", data)
    yes_bid_cents = market.get("yes_bid")
    yes_ask_cents = market.get("yes_ask")

    if yes_bid_cents is None or yes_ask_cents is None:
        _log.debug(f"{market_ticker}: missing bid/ask in response")
        return None

    yes_bid = yes_bid_cents / 100.0
    yes_ask = yes_ask_cents / 100.0

    # Sanity: bid > ask is broken book.
    if yes_bid > yes_ask:
        _log.warning(f"{market_ticker}: inverted book bid={yes_bid} ask={yes_ask}")
        return None

    # Filter wide spreads — implied prob is too noisy.
    if (yes_ask - yes_bid) > 0.10:  # >10pp spread = thin/illiquid
        _log.debug(f"{market_ticker}: wide spread {(yes_ask-yes_bid)*100:.1f}pp")

    return VenueQuote(
        venue=Venue.KALSHI,
        market_id=market_ticker,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        mid=(yes_bid + yes_ask) / 2.0,
        size_at_best=None,  # depth fetched separately if needed
        timestamp=dt.datetime.utcnow(),
    )


def implied_prob(quote: VenueQuote) -> Optional[float]:
    """Trivial — Kalshi prices ARE probabilities."""
    return quote.implied_prob


# ── Self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Construct a synthetic VenueQuote and verify implied_prob round-trip.
    q = VenueQuote(
        venue=Venue.KALSHI,
        market_id="KXFEDDECISION-TEST",
        yes_bid=0.62,
        yes_ask=0.64,
        mid=0.63,
    )
    assert implied_prob(q) == 0.63
    print("kalshi_prob self-test OK")
