"""Kalshi authenticated REST + WS client — RSA-PSS signing per v2 spec.

Pattern reused from Weather's `execution/kalshi_auth.py` (which has the
.env manual-parser fix). Same private key file works across engines since
they share one Kalshi account.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

from config import settings

_log = logging.getLogger(__name__)

# Manual .env loader — python-dotenv not guaranteed in venv.
def _load_dotenv_simple(path: str) -> None:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass

_load_dotenv_simple(str(Path(__file__).resolve().parent.parent / ".env"))


class KalshiAuthError(RuntimeError):
    pass


class KalshiClient:
    """Authenticated REST client. Thread-unsafe — one per scanner tick."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        private_key_path: Optional[str] = None,
        base_url: Optional[str] = None,
        rate_limit_sec: float = settings.KALSHI_RATE_LIMIT_SEC,
    ):
        self.base_url = (base_url or settings.KALSHI_API_BASE).rstrip("/")
        self.api_key = api_key or os.getenv("KALSHI_KEY_ID") or os.getenv("KALSHI_API_KEY")
        self.private_key_path = (
            private_key_path
            or os.getenv("KALSHI_PRIVATE_KEY_PATH")
            or settings.KALSHI_KEY_PATH
        )
        self.rate_limit_sec = rate_limit_sec
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._private_key = None
        self.authenticated = self._load_key()

    def _load_key(self) -> bool:
        if not self.api_key:
            _log.warning("KALSHI_KEY_ID / KALSHI_API_KEY not set in env")
            return False
        if not Path(self.private_key_path).exists():
            _log.warning(f"private key not found: {self.private_key_path}")
            return False
        try:
            from cryptography.hazmat.primitives import serialization
            with open(self.private_key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )
            return True
        except Exception as e:
            _log.warning(f"private key load failed: {e}")
            return False

    def _sign(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        sign_path = path.split("?", 1)[0]
        if not sign_path.startswith("/trade-api/v2"):
            sign_path = "/trade-api/v2" + sign_path
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{sign_path}".encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY":       self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.rate_limit_sec:
            time.sleep(self.rate_limit_sec - elapsed)
        self._last_request = time.time()

    def get(self, path: str, *, params: Optional[dict] = None) -> dict:
        """Authenticated GET for endpoints that need it. Incentive programs
        endpoint DOES NOT require auth, but portfolio/orders/balance do."""
        if not self.authenticated:
            raise KalshiAuthError("Client not authenticated — refusing GET")
        if params:
            from urllib.parse import urlencode
            full_path = f"{path}?{urlencode(params)}"
        else:
            full_path = path
        headers = self._sign("GET", full_path)
        self._throttle()
        r = self._session.get(self.base_url + full_path, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_unauth(self, path: str, *, params: Optional[dict] = None) -> dict:
        """Unauthenticated GET — works for market data + incentive programs."""
        if params:
            from urllib.parse import urlencode
            full_path = f"{path}?{urlencode(params)}"
        else:
            full_path = path
        self._throttle()
        r = self._session.get(self.base_url + full_path, timeout=10)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        if not self.authenticated:
            raise KalshiAuthError("Client not authenticated — refusing POST")
        headers = self._sign("POST", path)
        self._throttle()
        r = self._session.post(self.base_url + path, headers=headers,
                               data=json.dumps(body), timeout=10)
        r.raise_for_status()
        return r.json()

    def delete(self, path: str) -> dict:
        if not self.authenticated:
            raise KalshiAuthError("Client not authenticated — refusing DELETE")
        headers = self._sign("DELETE", path)
        self._throttle()
        r = self._session.delete(self.base_url + path, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json() if r.text else {}

    def get_balance(self) -> float:
        """Portfolio balance in dollars (Kalshi reports cents)."""
        data = self.get("/portfolio/balance")
        cents = data.get("balance", 0) or 0
        return float(cents) / 100.0
