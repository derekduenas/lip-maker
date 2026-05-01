"""Polymarket US authentication — Ed25519 keypair signing.

Confirmed auth spec from Polymarket developer portal:

  Headers:
    X-PM-Access-Key: <api key UUID>
    X-PM-Timestamp:  <unix milliseconds>
    X-PM-Signature:  <base64 ed25519 signature>

  Secret key format: base64-encoded 64-byte libsodium "secret key"
  (32-byte seed concatenated with 32-byte public key). We extract the
  32-byte seed for use with cryptography.hazmat.

ENV/CONFIG:
  PM_API_KEY_ID                — public access-key UUID
  PM_API_SECRET_KEY_B64        — base64 secret key (preferred)
  PM_API_PRIVATE_KEY_PATH      — path to file with base64 secret on
                                  one line, OR PEM-encoded key
  PM_REST_BASE                 — REST base URL (default: https://api.polymarket.us)
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from config import settings

_log = logging.getLogger(__name__)


class PMAuthError(Exception):
    pass


# ── .env loader (manual; matches lip-maker pattern) ─────────────────────
def _load_dotenv_simple(path: str) -> None:
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# Load .env from project root if present
_load_dotenv_simple(str(Path(__file__).resolve().parent.parent / ".env"))


def _load_signing_key():
    """Load Ed25519 signing key from base64 (preferred) or PEM file.

    Returns: (signing_key, public_bytes) where signing_key is an
    Ed25519PrivateKey instance.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise PMAuthError("pip install cryptography")

    # Path 1: env var with raw base64 secret
    b64 = os.getenv("PM_API_SECRET_KEY_B64", "").strip()

    # Path 2: file at PM_API_PRIVATE_KEY_PATH (preferred, less leaky than env)
    if not b64:
        # Default: config/polymarket_secret_key.b64
        default_path = Path(settings.PROJECT_ROOT) / "config" / "polymarket_secret_key.b64"
        key_path = os.getenv("PM_API_PRIVATE_KEY_PATH", str(default_path))
        if Path(key_path).exists():
            content = Path(key_path).read_text().strip()
            if "BEGIN" in content:
                # PEM format
                try:
                    sk = serialization.load_pem_private_key(content.encode(), password=None)
                    if not isinstance(sk, Ed25519PrivateKey):
                        raise PMAuthError(f"key is not Ed25519: {type(sk).__name__}")
                    return sk
                except Exception as e:
                    raise PMAuthError(f"PEM load failed: {e}")
            else:
                b64 = content
        else:
            raise PMAuthError(f"no key file found at {key_path}")

    # Decode base64 → bytes
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        raise PMAuthError(f"base64 decode failed: {e}")

    # libsodium format: 64 bytes (32-byte seed + 32-byte public)
    # cryptography expects: 32-byte seed
    if len(raw) == 64:
        seed = raw[:32]
    elif len(raw) == 32:
        seed = raw
    else:
        raise PMAuthError(f"unexpected key length {len(raw)} bytes (need 32 or 64)")

    try:
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as e:
        raise PMAuthError(f"Ed25519 key construction failed: {e}")


def _sign(key, payload: bytes) -> str:
    sig = key.sign(payload)
    return base64.b64encode(sig).decode()


class PolymarketClient:
    """Auth-signed REST client for Polymarket US.

    Per Polymarket developer portal:
      X-PM-Access-Key: keyId (UUID)
      X-PM-Timestamp:  unix MILLISECONDS as string
      X-PM-Signature:  base64 ed25519 signature

    Signature payload format (BEST GUESS, may need adjustment after
    first call observed):
      payload = f"{timestamp_ms}{method_upper}{path_with_qs}{body_str}"

    If 401 errors persist, try alternates:
      - "{timestamp_ms}\n{method}\n{path}\n{body}"
      - hash of body, not body itself
      - Lowercase method
    """

    def __init__(self,
                 api_key_id: str | None = None,
                 base_url: str | None = None):
        self.api_key_id = api_key_id or os.getenv("PM_API_KEY_ID") or settings.PM_API_KEY_ID
        self.base_url   = (base_url or settings.PM_REST_BASE).rstrip("/")
        if not self.api_key_id:
            _log.warning("PM_API_KEY_ID not set — auth'd requests will fail")
            self._key = None
        else:
            try:
                self._key = _load_signing_key()
                _log.info(f"Polymarket client ready  key_id={self.api_key_id[:8]}…")
            except PMAuthError as e:
                _log.warning(f"key load failed ({e})")
                self._key = None

    def _signed_headers(self, method: str, path_with_qs: str, body: str = "") -> dict:
        if not self._key or not self.api_key_id:
            raise PMAuthError("Client not authenticated")
        ts_ms = str(int(time.time() * 1000))
        payload = f"{ts_ms}{method.upper()}{path_with_qs}{body}".encode()
        sig = _sign(self._key, payload)
        return {
            "X-PM-Access-Key":  self.api_key_id,
            "X-PM-Timestamp":   ts_ms,
            "X-PM-Signature":   sig,
            "Content-Type":     "application/json",
            "Accept":           "application/json",
            "User-Agent":       "polymarket-maker/0.1",
        }

    def _request(self, method: str, path: str,
                 params: dict | None = None,
                 body: dict | None = None,
                 timeout: int = 10) -> dict:
        if params:
            qs = urllib.parse.urlencode(params)
            full_path = f"{path}?{qs}"
        else:
            full_path = path
        url = f"{self.base_url}{full_path}"
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        headers = self._signed_headers(method, full_path, body_str)
        data = body_str.encode() if body_str else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            import ssl
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                resp_body = r.read().decode()
                if not resp_body:
                    return {}
                return json.loads(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if hasattr(e, "read") else ""
            raise PMAuthError(f"HTTP {e.code} {method} {path}: {err_body[:300]}")
        except Exception as e:
            raise PMAuthError(f"request failed {method} {path}: {e}")

    # ── Public API ──────────────────────────────────────────────────────
    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, body=body)

    def delete(self, path: str, params: dict | None = None) -> dict:
        return self._request("DELETE", path, params=params)

    # ── Convenience wrappers ────────────────────────────────────────────
    def get_balance(self) -> float:
        try:
            r = self.get("/v1/portfolio/balance")
            for key in ("balance_usd", "balance", "available_balance", "usd"):
                if key in r:
                    return float(r[key])
            _log.warning(f"unknown balance response shape: {list(r.keys())}")
            return 0.0
        except PMAuthError:
            raise

    def list_resting_orders(self) -> list[dict]:
        r = self.get("/v1/portfolio/orders", params={"status": "resting", "limit": 200})
        return r.get("orders", []) or r.get("data", []) or []

    def list_positions(self) -> list[dict]:
        r = self.get("/v1/portfolio/positions", params={"limit": 200})
        return r.get("positions", []) or r.get("market_positions", []) or r.get("data", []) or []

    # ── Order placement (matches polymarket-us SDK schema) ──────────────
    def place_order(self,
                    market_slug: str,
                    intent: str,                    # 'ORDER_INTENT_BUY_LONG' | 'BUY_SHORT' | 'SELL_LONG' | 'SELL_SHORT'
                    price: float,                    # 0.01 - 0.99
                    quantity: int,
                    tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
                    order_type: str = "ORDER_TYPE_LIMIT") -> dict:
        """Place a limit order. Mirrors official polymarket-us SDK shape.

        Args:
          market_slug: e.g. 'chiefs-super-bowl' (the polymarket.us slug)
          intent: ORDER_INTENT_BUY_LONG  = buy YES
                  ORDER_INTENT_BUY_SHORT = buy NO (or sell YES short)
                  ORDER_INTENT_SELL_LONG = close long YES
                  ORDER_INTENT_SELL_SHORT = close short
          price: dollars (e.g. 0.55 = 55 cents)
          quantity: contracts
          tif: TIME_IN_FORCE_GOOD_TILL_CANCEL | FILL_OR_KILL | IMMEDIATE_OR_CANCEL

        Returns: order response (includes order_id on success).
        Raises PMAuthError on HTTP error.
        """
        body = {
            "marketSlug": market_slug,
            "intent":     intent,
            "type":       order_type,
            "price":      {"value": f"{price:.3f}", "currency": "USD"},
            "quantity":   int(quantity),
            "tif":        tif,
        }
        return self.post("/v1/orders", body=body)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel single order by id."""
        return self.delete(f"/v1/orders/{order_id}")

    def cancel_all_orders(self, market_slug: str | None = None) -> dict:
        """Cancel all open orders, optionally scoped to one market."""
        params = {"marketSlug": market_slug} if market_slug else None
        return self.delete("/v1/orders", params=params)

    # ── Convenience helpers for the quote loop ──────────────────────────
    def buy_yes(self, market_slug: str, price: float, quantity: int,
                tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL") -> dict:
        return self.place_order(market_slug, "ORDER_INTENT_BUY_LONG",
                                price, quantity, tif=tif)

    def buy_no(self, market_slug: str, price: float, quantity: int,
               tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL") -> dict:
        return self.place_order(market_slug, "ORDER_INTENT_BUY_SHORT",
                                price, quantity, tif=tif)
