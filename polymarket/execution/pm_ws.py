"""Polymarket US WebSocket client — real-time book stream.

Replaces 30s REST polling with sub-second push notifications. Critical
for fast-moving markets (sports/crypto live) where 30s = miss every
material price move.

USAGE:
  from execution.pm_ws import PMBookStream
  stream = PMBookStream(key_id=..., secret_key=..., on_update=callback)
  stream.subscribe(['aec-mlb-sf-phi-2026-04-30', 'tc-temp-laxhigh-...'])
  stream.start()  # non-blocking; runs in background thread

  # Read latest book for any subscribed slug:
  book = stream.books.get('aec-mlb-sf-phi-2026-04-30')
  if book:
      best_bid = book.best_yes_bid()

ARCHITECTURE:
  - Uses SDK's MarketsWebSocket (handles Ed25519 auth headers)
  - 10 instruments per WS connection (Polymarket-side cap)
  - Sharded across multiple WS connections if N > 10
  - Auto-reconnect with exp backoff
  - In-memory book state per slug, callback on each delta
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from polymarket_us.websocket import MarketsWebSocket

_log = logging.getLogger(__name__)

INSTRUMENTS_PER_CONN = 10  # Polymarket-side cap


@dataclass
class BookState:
    """Latest book state for one Polymarket slug."""
    slug:        str
    bids:        list = field(default_factory=list)  # [{px, qty}, ...]
    offers:      list = field(default_factory=list)
    state:       str = ""
    last_update: float = 0.0

    def best_yes_bid(self) -> float | None:
        if not self.bids:
            return None
        try:
            return float(self.bids[0].get("px", {}).get("value"))
        except (TypeError, ValueError, AttributeError):
            return None

    def best_yes_ask(self) -> float | None:
        if not self.offers:
            return None
        try:
            return float(self.offers[0].get("px", {}).get("value"))
        except (TypeError, ValueError, AttributeError):
            return None

    def top_bid_size(self) -> int:
        if not self.bids:
            return 0
        try:
            return int(float(self.bids[0].get("qty", 0)))
        except (TypeError, ValueError):
            return 0

    def top_ask_size(self) -> int:
        if not self.offers:
            return 0
        try:
            return int(float(self.offers[0].get("qty", 0)))
        except (TypeError, ValueError):
            return 0


class PMBookStream:
    """Multi-shard WebSocket book stream for Polymarket US.

    Auth is handled by the SDK's MarketsWebSocket (Ed25519 headers
    via create_auth_headers). Caller passes key_id + secret_key.
    """

    def __init__(self,
                 key_id: str,
                 secret_key: str,
                 on_update: Callable[[BookState], None] | None = None):
        self.key_id = key_id
        self.secret_key = secret_key
        self.books: dict[str, BookState] = {}
        self.on_update = on_update
        self._stop = False
        self._thread: threading.Thread | None = None
        self._slugs: list[str] = []

    def subscribe(self, slugs: list[str]) -> None:
        self._slugs = list(slugs)

    def _shards(self) -> list[list[str]]:
        return [self._slugs[i:i+INSTRUMENTS_PER_CONN]
                for i in range(0, len(self._slugs), INSTRUMENTS_PER_CONN)]

    def _on_market_data(self, message: dict) -> None:
        """Handle a parsed marketData WS message."""
        payload = message.get("marketData") or {}
        slug = payload.get("marketSlug")
        if not slug:
            return
        bids = payload.get("bids", [])
        offers = payload.get("offers", [])
        if not (bids or offers):
            return
        book = self.books.setdefault(slug, BookState(slug=slug))
        book.bids = bids
        book.offers = offers
        book.state = payload.get("state", book.state)
        book.last_update = time.time()
        if self.on_update:
            try:
                self.on_update(book)
            except Exception as e:
                _log.debug(f"on_update cb err: {e}")

    async def _shard_loop(self, slugs: list[str]) -> None:
        backoff = 1.0
        while not self._stop:
            ws = MarketsWebSocket(key_id=self.key_id, secret_key=self.secret_key)
            ws.on("market_data", self._on_market_data)

            closed = asyncio.Event()
            err_holder = {"err": None}
            ws.on("close", lambda: closed.set())
            def _on_err(e):
                err_holder["err"] = e
                closed.set()
            ws.on("error", _on_err)

            try:
                await ws.connect()
                req_id = uuid.uuid4().hex
                await ws.subscribe_market_data(req_id, slugs)
                _log.info(f"PM WS subscribed shard ({len(slugs)} slugs) "
                          f"req_id={req_id[:8]}")
                backoff = 1.0
                # Wait until close or stop
                while not self._stop and not closed.is_set():
                    try:
                        await asyncio.wait_for(closed.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                if err_holder["err"]:
                    raise err_holder["err"]
            except Exception as e:
                _log.warning(f"PM WS shard error: {e}, reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass

    async def _main(self) -> None:
        shards = self._shards()
        if not shards:
            _log.warning("PM WS: no slugs to subscribe")
            return
        _log.info(f"PM WS: starting {len(shards)} shards for {len(self._slugs)} slugs")
        tasks = [asyncio.create_task(self._shard_loop(s)) for s in shards]
        await asyncio.gather(*tasks, return_exceptions=True)

    def start(self) -> None:
        """Run WS loop in a background thread (so caller can keep doing
        sync REST work)."""
        def _runner():
            asyncio.run(self._main())
        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True


# Convenience: how stale is a book?
def book_age_sec(book: BookState) -> float:
    if not book or not book.last_update:
        return 999.0
    return time.time() - book.last_update
