"""Gemini Predictions implementation of the Venue interface.

CFTC-regulated DCM (Gemini Titan LLC), launched Feb 2026. Active Maker Rebate
Program with PROMO rates through May 9, 2026.

Authentication: HMAC-SHA384 over base64-encoded nonce payload (standard
Gemini API pattern, different from Kalshi's RSA-PSS).

Order submission is WebSocket-based (unlike Kalshi's REST). This class exposes
the same Venue interface but uses a WS client internally.

## DOCUMENTED GAPS (verify at integration time)

The public docs (docs.gemini.com/prediction-markets) fully specify:
  ✓ Auth mechanism (HMAC-SHA384)
  ✓ Order.place + order.cancel WS methods
  ✓ Maker rebate formula + eligibility
  ✓ WebSocket URL + auth-on-handshake

But DO NOT specify:
  ✗ REST endpoint for orderbook depth (only WS {symbol}@bookTicker)
  ✗ REST endpoint for fills / trade history
  ✗ REST endpoint for balance
  ✗ Sandbox / paper environment
  ✗ Rate limits
  ✗ Post-only flag syntax

Anywhere marked `# TODO_GEMINI` needs verification via either:
  (a) Gemini support ticket
  (b) API schema dump at /prediction-markets/~schemas (mentioned in docs)
  (c) Reverse-engineering from the web UI's network calls

Safe defaults applied where possible. Nothing actually hits Gemini until
credentials are loaded via environment variables.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Awaitable, Callable, Optional

import requests

from venue.base import (
    Venue, Position, Fill, OrderResult, OrderbookSnapshot, BookLevel,
    MarketMetadata, register_venue,
)

_log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
GEMINI_REST_BASE = "https://api.gemini.com/v1/prediction-markets"
GEMINI_WS_URL    = "wss://ws.gemini.com"
# Standard Gemini (non-predictions) API, which may host balance/fills endpoints
# not yet documented under /prediction-markets. TODO_GEMINI: verify.
GEMINI_STANDARD_REST = "https://api.gemini.com/v1"


def _load_credentials() -> tuple[Optional[str], Optional[str]]:
    """Return (api_key, api_secret). Both None if not configured."""
    key = os.getenv("GEMINI_PREDICTIONS_API_KEY")
    sec = os.getenv("GEMINI_PREDICTIONS_API_SECRET")
    return key, sec


def _sign_payload(secret: str, payload_b64: str) -> str:
    """HMAC-SHA384 over the base64-encoded payload. Returns hex digest."""
    return hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha384,
    ).hexdigest()


def _build_auth_headers(api_key: str, api_secret: str,
                         payload: dict) -> dict[str, str]:
    """Build the 4 required Gemini auth headers for a request."""
    nonce = int(time.time() * 1000)  # ms; docs say "unix timestamp" but ms is safer vs replay
    payload = {**payload, "nonce": nonce}
    raw = json.dumps(payload).encode("utf-8")
    payload_b64 = base64.b64encode(raw).decode("utf-8")
    sig = _sign_payload(api_secret, payload_b64)
    return {
        "X-GEMINI-APIKEY":    api_key,
        "X-GEMINI-PAYLOAD":   payload_b64,
        "X-GEMINI-SIGNATURE": sig,
        "Content-Type":       "text/plain",   # Gemini standard
        "Content-Length":     "0",
        "Cache-Control":      "no-cache",
    }


@register_venue
class GeminiVenue(Venue):
    name = "gemini"

    def __init__(self):
        self._api_key, self._api_secret = _load_credentials()
        self._ws = None

    def _authed_post(self, path: str, payload: Optional[dict] = None) -> dict:
        """POST an authenticated request. Payload goes in the body header trick."""
        if not self._api_key or not self._api_secret:
            raise RuntimeError(
                "Gemini credentials not configured. Set GEMINI_PREDICTIONS_API_KEY "
                "and GEMINI_PREDICTIONS_API_SECRET environment variables."
            )
        payload = payload or {}
        payload = {**payload, "request": path}
        headers = _build_auth_headers(self._api_key, self._api_secret, payload)
        url = f"https://api.gemini.com{path}"
        resp = requests.post(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── Account state ──

    def get_balance(self) -> float:
        """TODO_GEMINI: verify exact endpoint. Docs reference WS balance@account
        stream but REST path not explicit. Best guess: /v1/balances (standard Gemini)."""
        r = self._authed_post("/v1/balances")
        # Gemini /v1/balances returns list of {currency, amount, available, ...}
        for bal in r:
            if bal.get("currency") in ("USD", "usd"):
                return float(bal.get("available", 0))
        return 0.0

    def get_positions(self) -> list[Position]:
        """POST /prediction-markets/positions per docs."""
        r = self._authed_post("/v1/prediction-markets/positions")
        out: list[Position] = []
        for p in r:
            # TODO_GEMINI: exact schema unclear from docs. Common fields expected:
            # symbol, outcome (YES/NO), quantity, avgPrice, realizedPnl
            outcome = (p.get("eventOutcome") or p.get("outcome", "")).upper()
            qty = int(float(p.get("quantity", 0)))
            if qty == 0:
                continue
            net = qty if outcome == "YES" else -qty
            avg_price_dollars = float(p.get("avgPrice", 0))
            out.append(Position(
                ticker=p.get("symbol", ""),
                net_contracts=net,
                avg_entry_cents=int(round(avg_price_dollars * 100)),
                market_exposure_usd=abs(net) * avg_price_dollars,
                realized_pnl_usd=float(p.get("realizedPnl", 0)),
            ))
        return out

    def get_fills(self, since_iso: Optional[str] = None,
                  limit: int = 200) -> list[Fill]:
        """TODO_GEMINI: endpoint unclear. Likely /v1/mytrades with symbol filter,
        or separate /prediction-markets/trades. Best guess: the former."""
        raise NotImplementedError(
            "GeminiVenue.get_fills — endpoint not confirmed in public docs. "
            "Verify via schema dump or Gemini support before implementing."
        )

    # ── Orders ──

    def place_order(self, ticker: str, side: str, price_cents: int,
                    size_contracts: int, post_only: bool = True) -> OrderResult:
        """Place via WebSocket method order.place.

        Note: Gemini docs show orders placed over WS, not REST. This method
        either (a) opens a short-lived WS session, (b) submits the message,
        (c) awaits confirmation. For maker discipline, we rely on placing
        BELOW best ask (buys) / ABOVE best bid (sells) since docs mention
        no explicit post-only flag. TODO_GEMINI: confirm post-only syntax.

        Gemini prices are DOLLARS (e.g. "0.27"), not cents — convert.
        Gemini uses eventOutcome YES/NO + side BUY/SELL, while Kalshi uses
        side yes/no. Adaptation: our `side="yes"` → eventOutcome="YES", side="BUY"
        (we're buying YES contracts). Never SELL because we're a maker.
        """
        if side not in ("yes", "no"):
            return OrderResult(success=False, error=f"invalid side: {side}")
        price_dollars = price_cents / 100.0
        if not (0.01 <= price_dollars <= 0.99):
            return OrderResult(success=False,
                               error=f"price {price_dollars} outside $0.01-$0.99 band")

        # TODO_GEMINI: verify WS-based order placement. Stub REST fallback:
        body = {
            "symbol":         ticker,
            "side":           "BUY",
            "type":           "LIMIT",
            "timeInForce":    "GTC",
            "price":          f"{price_dollars:.2f}",
            "quantity":       str(int(size_contracts)),
            "eventOutcome":   side.upper(),
            "clientOrderId":  f"innait-{uuid.uuid4().hex[:12]}",
        }
        try:
            r = self._authed_post("/v1/prediction-markets/order/new", body)
            order_id = r.get("orderId") or r.get("order_id")
            if order_id:
                return OrderResult(success=True, order_id=str(order_id), raw=r)
            return OrderResult(success=False, error="no order_id in response", raw=r)
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._authed_post("/v1/prediction-markets/order/cancel",
                              {"orderId": str(order_id)})
            return True
        except Exception as e:
            _log.warning(f"gemini cancel failed {order_id}: {e}")
            return False

    def cancel_all_for_ticker(self, ticker: str) -> int:
        """TODO_GEMINI: bulk cancel-by-symbol endpoint not documented.
        Fallback: fetch open orders, cancel each.
        """
        raise NotImplementedError(
            "GeminiVenue.cancel_all_for_ticker — need to verify bulk-cancel support."
        )

    # ── Market data ──

    def get_market_metadata(self, ticker: str) -> MarketMetadata:
        """Public endpoint, no auth needed. /prediction-markets/events?symbol=..."""
        r = requests.get(
            f"{GEMINI_REST_BASE}/events",
            params={"symbol": ticker},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        events = data if isinstance(data, list) else data.get("events", [data])
        if not events:
            raise ValueError(f"no Gemini market for ticker: {ticker}")
        m = events[0]
        # TODO_GEMINI: field names are guesses based on typical exchange schemas.
        return MarketMetadata(
            ticker=ticker,
            tick_size_cents=1,                              # typical $0.01 tick
            min_size=1,
            close_time=m.get("expirationTime") or m.get("closeTime"),
            settlement_rule=m.get("rules") or m.get("description"),
            extra={
                "category":    m.get("category"),
                "status":      m.get("status"),
                "raw":         m,
            },
        )

    async def stream_orderbook(self, tickers: list[str],
                                on_update: Callable[[OrderbookSnapshot], Awaitable[None]]
                               ) -> None:
        """WebSocket streaming via {symbol}@bookTicker subscription.

        NOT YET IMPLEMENTED — requires websockets library integration + auth
        on handshake. Structure:
          1. Open WS with auth headers
          2. Subscribe: {"method": "SUBSCRIBE", "params": [f"{t}@bookTicker" for t in tickers]}
          3. On message, parse to OrderbookSnapshot and call on_update
          4. Handle reconnect

        Estimated: 3-4 hours once credentials are live. Mirrors kalshi_ws.py
        pattern.
        """
        raise NotImplementedError(
            "GeminiVenue.stream_orderbook — will implement alongside credential setup."
        )


# ─── Helper: public market discovery (no auth needed) ─────────────────

def list_active_markets() -> list[dict]:
    """Public endpoint. Lists currently-active Gemini Predictions markets."""
    r = requests.get(
        f"{GEMINI_REST_BASE}/events",
        params={"status": "active", "limit": 100},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("events", [])
