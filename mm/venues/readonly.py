"""Production Kalshi market data, read-only.

Paper mode uses this client when ``KALSHI_PROD_READ_KEY_ID`` and
``KALSHI_PROD_READ_KEY_PATH`` are set. Those variables are not the demo
order key. This class is not an order transport: it is a
``MarketDataReader``. ``KalshiAdapter``, ``SafeSender``, ``DemoPoster``,
and the quote manager refuse it before any socket opens.

Allowed: GET markets, events, series, order books, trades,
incentive programs, and exchange status. The websocket may subscribe to
``orderbook_delta``, ``ticker`` and ``trade`` for named tickers, and to
``market_lifecycle_v2`` (market-level lifecycle events, all markets, no
ticker filter). Every Kalshi websocket connection is authenticated with the
key's signed headers, private channels included, so the read-only key opens
the same socket; what keeps it read-only is that only these market-data
channels are ever subscribed. It may also remove tickers from, or end,
subscriptions it opened itself (``update_subscription`` with
``delete_markets``, ``unsubscribe``).

Anything else logs and exits. No POST, PUT, DELETE, PATCH, portfolio
route, or private channel is sent.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from mm.venues.kalshi_rest import API_PREFIX, PRODUCTION_HOSTS, signing_path

_log = logging.getLogger("lip.readonly")

PROD_READ_KEY_ID = "KALSHI_PROD_READ_KEY_ID"
PROD_READ_KEY_PATH = "KALSHI_PROD_READ_KEY_PATH"
PROD_REST = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_REST = "https://demo-api.kalshi.co/trade-api/v2"
DEMO_WS = "wss://demo-api.kalshi.co/trade-api/ws/v2"
DEMO_BOOKS_FLAG = "demo-books: results not representative"
PROD_BOOKS_FLAG = "production-books"

# Per-ticker market data channels.
TICKER_WS_CHANNELS = frozenset({"orderbook_delta", "ticker", "trade"})
# Market/event lifecycle (created, determined, settled, ...). Market data, not
# account data; it takes no market filter ("market_ticker filters are not
# supported", docs.kalshi.com/websockets/market-&-event-lifecycle).
LIFECYCLE_WS_CHANNEL = "market_lifecycle_v2"
PUBLIC_WS_CHANNELS = TICKER_WS_CHANNELS | {LIFECYCLE_WS_CHANNEL}

_FORBIDDEN_SNIPPETS = (
    "/portfolio",
    "/orders",
    "/fills",
    "/positions",
    "order_group",
    "/communications",
)


class ReadOnlyViolation(SystemExit):
    """A write or a private route was attempted on the read-only client.

    ``SystemExit`` so an uncaught attempt ends the process. The code is 3.
    """

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(3)


def _ws_ping() -> tuple[float, float]:
    """Websocket keepalive (LIP_WS_PING_INTERVAL / LIP_WS_PING_TIMEOUT, default 20/20)."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return float(default)
    return _f("LIP_WS_PING_INTERVAL", 20.0), _f("LIP_WS_PING_TIMEOUT", 20.0)


class ReadOnlyHTTPError(ReadOnlyViolation):
    """An allowed public GET came back HTTP 4xx/5xx.

    Still a ``ReadOnlyViolation`` (exit code 3) for callers that do not
    handle it. The read-only book driver catches this subclass, backs off,
    and retries. Route and verb refusals stay plain ``ReadOnlyViolation``
    and still end the process.
    """

    def __init__(self, reason: str, status: int) -> None:
        self.status = int(status)
        super().__init__(reason)


class MarketDataReader:
    """Nominal type for production book reads. Order code is not this class."""

    read_only_market_data = True
    writes_orders = False


def _refuse(reason: str) -> None:
    _log.error("read-only Kalshi refused %s", reason)
    raise ReadOnlyViolation(reason)


def reject_market_data_reader(obj) -> None:
    """Order paths call this. A reader is not a transport they can hold."""
    if obj is None:
        return
    if isinstance(obj, MarketDataReader) or getattr(obj, "read_only_market_data", False) is True:
        _refuse("order path cannot use the production read-only transport")
    owner = getattr(obj, "__self__", None)
    if owner is not None and owner is not obj:
        if isinstance(owner, MarketDataReader) or getattr(owner, "read_only_market_data", False) is True:
            _refuse("order path cannot use the production read-only transport")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _relative(path: str) -> str:
    bare = signing_path(path).split("?", 1)[0]
    if bare.startswith(API_PREFIX):
        bare = bare[len(API_PREFIX):]
    if not bare.startswith("/"):
        bare = "/" + bare
    return bare


# Percent-encodings of '/', '\\', '.' and of '%' itself (double encoding).
_ENCODED_UNSAFE = ("%2f", "%5c", "%2e", "%25")


def path_has_traversal(path: str) -> bool:
    """True when the PATH part (before '?'/'#') could escape an allowlisted
    prefix once a server or proxy normalises it: a '.' or '..' segment, a
    backslash, or a percent-encoded '/', '\\', '.' or '%'. The query string
    is not inspected (it cannot change the route, and page tokens there are
    legitimately percent-encoded)."""
    bare = str(path or "").split("#", 1)[0].split("?", 1)[0]
    if "\\" in bare:
        return True
    low = bare.lower()
    if any(enc in low for enc in _ENCODED_UNSAFE):
        return True
    return any(seg in (".", "..") for seg in bare.split("/"))


def get_allowed(path: str) -> bool:
    """True only for the public market-data GETs (never a traversal path)."""
    if path_has_traversal(path):
        return False
    rel = _relative(path).lower()
    if any(snippet in rel for snippet in _FORBIDDEN_SNIPPETS):
        return False
    parts = [part for part in rel.split("/") if part]
    if not parts:
        return False
    head = parts[0]
    if head == "markets":
        if len(parts) == 1:
            return True
        if parts[1] == "trades" and len(parts) == 2:
            return True
        if len(parts) == 2:
            return True
        return len(parts) == 3 and parts[2] == "orderbook"
    if head == "events":
        return len(parts) <= 2
    if head == "series":
        return len(parts) <= 2
    if head == "incentive_programs":
        return len(parts) == 1
    if head == "exchange":
        return parts == ["exchange", "status"]
    if head == "orderbooks":
        return len(parts) <= 2
    return False


def book_source(environ: dict | None = None, *, force_demo: bool = False) -> dict:
    """Which books paper mode reads. Orders never use this URL."""
    env = os.environ if environ is None else environ
    key_id = str(env.get(PROD_READ_KEY_ID) or "").strip()
    key_path = str(env.get(PROD_READ_KEY_PATH) or "").strip()
    ready = bool(key_id and key_path and Path(key_path).is_file())
    if ready and not force_demo:
        return {
            "flag": PROD_BOOKS_FLAG,
            "representative": True,
            "reader": True,
            "rest_url": PROD_REST,
            "ws_url": PROD_WS,
            "key_id": key_id,
            "key_path": key_path,
        }
    return {
        "flag": DEMO_BOOKS_FLAG,
        "representative": False,
        "reader": False,
        "rest_url": DEMO_REST,
        "ws_url": DEMO_WS,
        "key_id": "",
        "key_path": "",
    }


def load_private_key(path: str):
    from cryptography.hazmat.primitives import serialization
    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=None)


class ReadOnlyKalshiTransport(MarketDataReader):
    """Signed GET client for the production host. Writes log and exit.

    ``session`` is injectable. Refusal paths return before ``session.request``.
    """

    def __init__(self, *, api_key: str, private_key, session=None,
                 base_url: str = PROD_REST) -> None:
        host = _host(base_url)
        if urlsplit(base_url).scheme != "https" or host not in PRODUCTION_HOSTS:
            _refuse(f"read-only client requires the production host, got {host or base_url}")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._private_key = private_key
        self._session = session
        self.allow_production = False
        self.writes_orders = False

    def sign_headers(self, method: str, path: str) -> dict:
        signed = signing_path(path)
        ts = str(int(__import__("time").time() * 1000))
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
        verb = (method or "").upper()
        if verb != "GET" or body is not None or not get_allowed(path):
            _refuse(f"{verb or method} {path}")
        from urllib.parse import urlencode
        rel = path
        if params:
            rel = f"{path}{'&' if '?' in path else '?'}{urlencode(params)}"
        if self._session is None:
            _refuse("no session")
        headers = self.sign_headers("GET", rel)
        url = self.base_url + (rel if rel.startswith("/") else "/" + rel)
        resp = self._session.request("GET", url, headers=headers, data=None, timeout=10)
        status = int(getattr(resp, "status_code", 0) or 0)
        payload = {}
        if hasattr(resp, "json"):
            try:
                payload = resp.json()
            except Exception:
                payload = {}
        if status >= 400:
            _log.warning("read-only Kalshi GET %s HTTP %s", path, status)
            raise ReadOnlyHTTPError(f"GET {path} HTTP {status}", status)
        return payload if isinstance(payload, dict) else {}

    def get(self, path: str, *, params: Optional[dict] = None) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Optional[dict] = None, **kwargs) -> dict:
        _refuse(f"POST {path}")

    def put(self, path: str, body: Optional[dict] = None, **kwargs) -> dict:
        _refuse(f"PUT {path}")

    def patch(self, path: str, body: Optional[dict] = None, **kwargs) -> dict:
        _refuse(f"PATCH {path}")

    def delete(self, path: str, **kwargs) -> dict:
        _refuse(f"DELETE {path}")

    def place(self, *args, **kwargs) -> dict:
        _refuse("place")

    def cancel(self, *args, **kwargs) -> dict:
        _refuse("cancel")

    def amend(self, *args, **kwargs) -> dict:
        _refuse("amend")

    def decrease(self, *args, **kwargs) -> dict:
        _refuse("decrease")

    def create_order_group(self, *args, **kwargs) -> dict:
        _refuse("create_order_group")

    def trigger_order_group(self, *args, **kwargs) -> dict:
        _refuse("trigger_order_group")


class ReadOnlyMarketSocket(MarketDataReader):
    """Production websocket. Subscribes only PUBLIC_WS_CHANNELS.

    Each command gets its own ``id``; the ``subscribed`` replies
    (``note_response``) map each subscription id (sid) to its channel and
    tickers, so ``unsubscribe_markets`` can remove tickers from the
    subscriptions this socket opened (and only those)."""

    def __init__(self, *, api_key: str, private_key, url: str = PROD_WS) -> None:
        host = _host(url)
        if urlsplit(url).scheme != "wss" or host not in PRODUCTION_HOSTS:
            _refuse(f"read-only websocket requires the production host, got {host or url}")
        self.url = url
        self.api_key = api_key
        self._private_key = private_key
        self._ws = None
        self.connects = 0
        self._next_id = 0
        self._pending: dict[int, list[str]] = {}   # subscribe id -> tickers
        self.sids: dict[int, dict] = {}            # sid -> {"channel", "tickers": set}

    def _cmd_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def command(self, channels: list[str], tickers: list[str] | None = None) -> dict:
        names = [str(channel) for channel in channels]
        bad = [name for name in names if name not in PUBLIC_WS_CHANNELS]
        if not names or bad:
            _refuse("ws channel " + ",".join(bad or ["empty"]))
        if LIFECYCLE_WS_CHANNEL in names:
            # no ticker filter on this channel: subscribe it alone, unfiltered
            if len(names) != 1 or tickers:
                _refuse(f"ws channel {LIFECYCLE_WS_CHANNEL} is subscribed alone, without tickers")
            return {"id": self._cmd_id(), "cmd": "subscribe", "params": {"channels": names}}
        cid = self._cmd_id()
        self._pending[cid] = list(tickers or [])
        while len(self._pending) > 1000:
            self._pending.pop(next(iter(self._pending)))
        return {
            "id": cid,
            "cmd": "subscribe",
            "params": {
                "channels": names,
                "market_tickers": list(tickers or []),
            },
        }

    def note_response(self, msg: dict) -> None:
        """Track sids from ``subscribed`` / ``unsubscribed`` replies."""
        kind = msg.get("type")
        if kind == "subscribed":
            body = msg.get("msg") or {}
            sid = body.get("sid")
            if sid is None:
                return
            row = self.sids.setdefault(int(sid), {"channel": str(body.get("channel") or ""),
                                                  "tickers": set()})
            row["tickers"].update(self._pending.get(msg.get("id"), []))
        elif kind == "unsubscribed" and msg.get("sid") is not None:
            self.sids.pop(int(msg["sid"]), None)

    async def unsubscribe_markets(self, tickers) -> list[dict]:
        """Remove ``tickers`` from every per-ticker subscription this socket
        opened: ``update_subscription`` / ``delete_markets`` per sid, or
        ``unsubscribe`` for a sid that would be left with no ticker. The
        lifecycle subscription is never touched. Returns the commands sent."""
        drop = {str(t) for t in tickers or ()}
        if not drop:
            return []
        if self._ws is None:
            _refuse("websocket is not connected")
        sent, empty = [], []
        for sid, row in sorted(self.sids.items()):
            if row["channel"] not in TICKER_WS_CHANNELS:
                continue
            hit = sorted(row["tickers"] & drop)
            if not hit:
                continue
            row["tickers"] -= drop
            if not row["tickers"]:
                empty.append(sid)
                continue
            cmd = {"id": self._cmd_id(), "cmd": "update_subscription",
                   "params": {"sids": [sid], "market_tickers": hit, "action": "delete_markets"}}
            await self._ws.send(json.dumps(cmd))
            sent.append(cmd)
        if empty:
            cmd = {"id": self._cmd_id(), "cmd": "unsubscribe", "params": {"sids": empty}}
            await self._ws.send(json.dumps(cmd))
            sent.append(cmd)
            for sid in empty:
                self.sids.pop(sid, None)
        return sent

    def auth_headers(self) -> dict:
        ts = str(int(__import__("time").time() * 1000))
        msg = f"{ts}GET/trade-api/ws/v2".encode()
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
        }

    async def connect(self) -> None:
        import websockets
        self.connects += 1
        headers = self.auth_headers()
        try:
            self._ws = await websockets.connect(
                self.url, additional_headers=headers, ping_interval=_ws_ping()[0], ping_timeout=_ws_ping()[1],
            )
        except TypeError:
            self._ws = await websockets.connect(
                self.url, extra_headers=headers, ping_interval=_ws_ping()[0], ping_timeout=_ws_ping()[1],
            )

    async def subscribe(self, channels: list[str], tickers: list[str] | None = None) -> dict:
        cmd = self.command(channels, tickers)
        if self._ws is None:
            _refuse("websocket is not connected")
        await self._ws.send(json.dumps(cmd))
        return cmd

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
