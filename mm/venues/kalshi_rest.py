"""Signed Kalshi REST transport behind the venue ``Transport`` interface.

Auth is the v2 scheme already used by ``execution/kalshi_auth.py``: RSA-PSS
SHA256 over ``timestamp_ms + METHOD + path``, and the path has no query
string. Official servers (OpenAPI 3.32.0, fetched 2026-10-01):

* demo: ``demo-api.kalshi.co``, ``external-api.demo.kalshi.co``
* production: ``api.elections.kalshi.com``, ``external-api.kalshi.com``

Demo hosts are allowed. Production hosts raise unless the caller passes
``allow_production=True``. That flag is not ``MAKER_ONLY_ENFORCEMENT_VERIFIED``
and it is not implied by the Kalshi post_only acknowledgement.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Optional
from urllib.parse import urlsplit

from mm.venues.base import TransportHTTPError

API_PREFIX = "/trade-api/v2"

# docs.kalshi.com OpenAPI servers, 2026-10-01.
DEMO_HOSTS = frozenset({
    "demo-api.kalshi.co",
    "external-api.demo.kalshi.co",
})
PRODUCTION_HOSTS = frozenset({
    "api.elections.kalshi.com",
    "external-api.kalshi.com",
})


class KalshiHostRejected(RuntimeError):
    """The URL is not an allowed Kalshi host for this process."""


def signing_path(path: str) -> str:
    """Path the signature covers. The query string is never included.

    Cancel puts ``?market_ticker=`` on the path. Signing that query would
    not match what Kalshi verifies (``execution/kalshi_auth.py`` strips it,
    and the demo client did the same).
    """
    bare = (path or "").split("?", 1)[0]
    if not bare.startswith("/"):
        bare = "/" + bare
    if bare == API_PREFIX or bare.startswith(API_PREFIX + "/"):
        return bare
    return API_PREFIX + bare


def assert_kalshi_host(url: str, *, allow_production: bool) -> None:
    """Refuse anything that is not https to a known Kalshi host.

    Production requires ``allow_production``. An unknown host is refused
    even when that flag is set.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise KalshiHostRejected(f"refusing non-https Kalshi URL {url!r}")
    if host in DEMO_HOSTS:
        return
    if host in PRODUCTION_HOSTS:
        if not allow_production:
            raise KalshiHostRejected(
                f"refusing production host {host}; construct KalshiRestTransport "
                "with allow_production=True. That flag is separate from maker "
                "enforcement and defaults off.")
        return
    raise KalshiHostRejected(f"refusing unknown host {host}")


class KalshiRestTransport:
    """``Transport`` that signs and sends. Redirects are not followed.

    ``session`` is injectable so tests do not open a socket. When omitted,
    a ``requests`` session is created on first use.
    """

    def __init__(self, *, base_url: str, api_key: str, private_key,
                 allow_production: bool = False, session=None) -> None:
        assert_kalshi_host(base_url, allow_production=allow_production)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._private_key = private_key
        self.allow_production = bool(allow_production)
        self._session = session
        self.last_signed_path = ""
        self.writes_orders = True
        self.read_only_market_data = False

    def _session_or_create(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def sign_headers(self, method: str, path: str) -> dict:
        signed = signing_path(path)
        self.last_signed_path = signed
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{signed}".encode()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        sig = self._private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, *,
                body: Optional[dict] = None,
                params: Optional[dict] = None) -> dict:
        from urllib.parse import urlencode
        rel = path
        if params:
            q = urlencode(params)
            rel = f"{path}{'&' if '?' in path else '?'}{q}"
        url = self.base_url + rel
        assert_kalshi_host(self.base_url, allow_production=self.allow_production)
        assert_kalshi_host(url, allow_production=self.allow_production)
        headers = self.sign_headers(method, rel)
        data = json.dumps(body) if body is not None else None
        resp = self._session_or_create().request(
            method.upper(), url, headers=headers, data=data,
            timeout=10, allow_redirects=False,
        )
        final = getattr(resp, "url", url) or url
        req_host = (urlsplit(url).hostname or "").lower()
        final_host = (urlsplit(final).hostname or "").lower()
        if final_host != req_host:
            raise KalshiHostRejected(
                f"refusing response host {final_host}; request was {req_host}")
        status = int(getattr(resp, "status_code", 0) or 0)
        if 300 <= status < 400:
            raise KalshiHostRejected(f"refusing redirect {status} from {url}")
        payload = _json_body(resp)
        if status >= 400:
            raise TransportHTTPError(status, payload, method=method, path=rel)
        return payload if isinstance(payload, dict) else {}


def _json_body(resp) -> dict:
    text = getattr(resp, "text", "") or ""
    if not text:
        return {}
    try:
        parsed = resp.json()
    except Exception:
        return {"raw": text}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}
