"""Public REST orderbook feed, shaped like the WebSocket feed.

Why
---
The operating loop is WebSocket-driven, and Kalshi's WS requires an
authenticated key (an unauthenticated handshake is rejected with HTTP 401).
The REST orderbook endpoint, `/markets/{ticker}/orderbook`, is public. So a
machine without trading credentials can still run the whole loop — the same
discovery, the same economic selection, the same accrual and exit logic —
against REAL current market data.

What this is NOT
----------------
This is a weaker instrument than the WS feed, and the difference matters for
what any run using it can claim:

  * These are periodic SNAPSHOTS, not sequenced deltas. There is no seq, so
    a gap cannot be detected; we mark the book stale on fetch failure only.
  * `delta_count` stays 0 and `last_seq` stays 0, truthfully. Nothing here
    fabricates a delta stream.
  * Between two polls the book can move and return, and we would never see
    it. Queue position, time priority and the exact instant of a cross are
    therefore NOT observable from this feed.
  * Consequently, fills simulated on top of it are coarser than fills
    simulated on WS data, and a profitability figure derived from it must
    say so. It is evidence about quoting decisions and book state; it is
    weaker evidence about execution.

Every response is written to the capture log with its fetch timestamp, so a
later replay can be checked against exactly what we saw.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

from execution.kalshi_ws import BookLevel, BookState

_log = logging.getLogger(__name__)

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _ssl_context() -> ssl.SSLContext:
    """A verified context. macOS python.org builds ship no default CA file,
    so fall back to certifi rather than disabling verification."""
    try:
        ctx = ssl.create_default_context()
        if ssl.get_default_verify_paths().cafile:
            return ctx
    except Exception:
        pass
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def _cents(price_str) -> int:
    """'0.0100' dollars -> 1 cent. Kalshi quotes fixed-point dollars here."""
    return int(round(float(price_str) * 100))


class RestBookFeed:
    """Drop-in for KalshiWS covering what PaperRunner actually uses:
    `books`, `connected`, `on_update`, `subscribe_orderbook`."""

    def __init__(self, *, poll_interval_sec: float = 5.0,
                 capture_path: Optional[str] = None,
                 max_concurrency: int = 4):
        self.books: dict[str, BookState] = {}
        self.connected: bool = True
        self.poll_interval_sec = poll_interval_sec
        self._tickers: list[str] = []
        self._cb: Optional[Callable] = None
        self._ctx = _ssl_context()
        self._sem = asyncio.Semaphore(max_concurrency)
        self._capture = Path(capture_path) if capture_path else None
        if self._capture:
            self._capture.parent.mkdir(parents=True, exist_ok=True)
        self.polls = 0
        self.fetch_errors = 0
        self.source = "rest_snapshot"

    def on_update(self, cb: Callable) -> None:
        self._cb = cb

    async def subscribe_orderbook(self, tickers: Iterable[str]) -> None:
        for t in tickers:
            if t not in self._tickers:
                self._tickers.append(t)
        _log.info(f"rest feed: tracking {len(self._tickers)} markets "
                  f"@{self.poll_interval_sec}s")

    # ── fetching ──────────────────────────────────────────────────────
    def _fetch_sync(self, ticker: str) -> Optional[dict]:
        url = f"{API_BASE}/markets/{ticker}/orderbook"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20, context=self._ctx) as r:
            return json.loads(r.read())

    def _record(self, ticker: str, payload: dict, ts: float) -> None:
        if not self._capture:
            return
        try:
            with self._capture.open("a") as fh:
                fh.write(json.dumps({
                    "kind": "orderbook_snapshot", "source": self.source,
                    "market_ticker": ticker, "fetched_utc": ts,
                    "payload": payload}) + "\n")
        except Exception as e:
            _log.debug(f"capture write failed: {e}")

    def _to_book(self, ticker: str, payload: dict, ts: float) -> BookState:
        inner = payload.get("orderbook_fp") or payload.get("orderbook") or {}
        yes = inner.get("yes_dollars") or inner.get("yes") or []
        no = inner.get("no_dollars") or inner.get("no") or []

        def levels(rows):
            out = []
            for row in rows or []:
                try:
                    out.append(BookLevel(_cents(row[0]), float(row[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            # BookState expects bids sorted best-first.
            return sorted(out, key=lambda l: -l.price_cents)

        b = self.books.get(ticker) or BookState(market_ticker=ticker)
        b.yes_bids = levels(yes)
        b.no_bids = levels(no)
        b.last_update_ts = ts
        b.snapshot_count += 1          # every poll IS a snapshot
        b.stale = False
        b.stale_reason = ""
        # last_seq / delta_count deliberately untouched: this feed has no
        # sequence and produces no deltas. Claiming otherwise would let a
        # consumer believe it can detect gaps.
        self.books[ticker] = b
        return b

    async def _poll_one(self, ticker: str) -> None:
        async with self._sem:
            ts = time.time()
            try:
                payload = await asyncio.to_thread(self._fetch_sync, ticker)
            except Exception as e:
                self.fetch_errors += 1
                b = self.books.get(ticker)
                if b is not None:
                    # We do not know the current book. Mark it stale so the
                    # runner pulls quotes rather than trading on an old one.
                    b.stale = True
                    b.stale_reason = "rest_fetch_failed"
                    b.stale_since_ts = ts
                _log.debug(f"rest fetch failed {ticker}: {e}")
                return
            if payload is None:
                return
            self._record(ticker, payload, ts)
            book = self._to_book(ticker, payload, ts)
            if self._cb is not None:
                res = self._cb(book)
                if asyncio.iscoroutine(res):
                    await res

    async def run(self, stop_after_sec: Optional[float] = None) -> None:
        started = time.time()
        while True:
            if stop_after_sec is not None and time.time() - started >= stop_after_sec:
                return
            if not self._tickers:
                await asyncio.sleep(0.5)
                continue
            self.polls += 1
            await asyncio.gather(*(self._poll_one(t) for t in self._tickers),
                                 return_exceptions=True)
            await asyncio.sleep(self.poll_interval_sec)
