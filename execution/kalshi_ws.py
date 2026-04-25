"""Kalshi WebSocket orderbook reader.

Subscribes to orderbook_snapshot + orderbook_delta channels for a given
list of market tickers. Maintains an in-memory book per market. Handles
sequence-number gaps by requesting a fresh snapshot on mismatch.

Kalshi WS protocol (per docs.kalshi.com/websockets/orderbook-updates):
  - Connect to wss://api.elections.kalshi.com/trade-api/ws/v2
  - Send auth via KALSHI-ACCESS-* headers (RSA-PSS signed on path /trade-api/ws/v2)
  - Send subscribe command with channel + market_tickers
  - Receive `orderbook_snapshot` with full book state (yes and no levels)
  - Receive `orderbook_delta` messages with incremental updates
  - Each message has a `seq` field; sequential seq per subscription
  - Gap in seq → re-subscribe (triggers fresh snapshot)

Usage:
    async def on_book(book: BookState): ...
    ws = KalshiWS()
    await ws.connect()
    await ws.subscribe_orderbook(["MKT-A", "MKT-B"], on_update=on_book)
    # runs forever; on_book called on every book change
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets

from config import settings
from execution.kalshi_auth import _load_dotenv_simple
import os

# Ensure .env is loaded (kalshi_auth import does this; belt-and-suspenders).
_load_dotenv_simple(str(Path(__file__).resolve().parent.parent / ".env"))

_log = logging.getLogger(__name__)


@dataclass
class BookLevel:
    """A single price level on one side of the book."""
    price_cents: int
    size: int  # contracts


@dataclass
class BookState:
    """Snapshot of an orderbook at a moment. Yes and no are mirror-priced:
    yes_price + no_price = $1 (both sum to 100 cents). Kalshi sends separate
    yes_bids / no_bids arrays; we keep both."""
    market_ticker: str
    yes_bids: list[BookLevel] = field(default_factory=list)  # sorted desc by price
    yes_asks: list[BookLevel] = field(default_factory=list)  # sorted asc by price
    no_bids:  list[BookLevel] = field(default_factory=list)
    no_asks:  list[BookLevel] = field(default_factory=list)
    last_seq: int = 0
    last_update_ts: float = 0.0
    snapshot_count: int = 0
    delta_count: int = 0

    def best_yes_bid(self) -> Optional[BookLevel]:
        return self.yes_bids[0] if self.yes_bids else None

    def best_yes_ask(self) -> Optional[BookLevel]:
        return self.yes_asks[0] if self.yes_asks else None

    def best_no_bid(self) -> Optional[BookLevel]:
        return self.no_bids[0] if self.no_bids else None

    def best_no_ask(self) -> Optional[BookLevel]:
        return self.no_asks[0] if self.no_asks else None

    def spread_cents(self) -> Optional[int]:
        by = self.best_yes_bid(); ya = self.best_yes_ask()
        if by is None or ya is None:
            return None
        return ya.price_cents - by.price_cents

    def __repr__(self) -> str:
        by = self.best_yes_bid(); ya = self.best_yes_ask()
        byp = by.price_cents if by else None; yap = ya.price_cents if ya else None
        bys = by.size if by else None; yas = ya.size if ya else None
        return (f"Book({self.market_ticker}: "
                f"yes_bid={byp}@{bys} ask={yap}@{yas} "
                f"seq={self.last_seq} snap={self.snapshot_count} delta={self.delta_count})")


BookCallback = Callable[[BookState], Awaitable[None]]


class KalshiWS:
    """Authenticated Kalshi WebSocket client with orderbook maintenance."""

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        private_key_path: Optional[str] = None,
    ):
        self.url = url or settings.KALSHI_WS_URL
        self.api_key = api_key or os.getenv("KALSHI_KEY_ID") or os.getenv("KALSHI_API_KEY")
        self.private_key_path = (
            private_key_path
            or os.getenv("KALSHI_PRIVATE_KEY_PATH")
            or settings.KALSHI_KEY_PATH
        )
        self.books: dict[str, BookState] = {}  # market_ticker -> BookState
        self._cmd_id = 0
        self._sid_to_tickers: dict[int, list[str]] = {}  # subscription id -> tickers
        self._stop = False
        self._ws = None
        self._private_key = None
        self._callbacks: list[BookCallback] = []
        # 2026-04-22 (Architect audit): reconnect callbacks fire after a
        # successful reconnect+resubscribe so consumers can purge stale
        # in-memory state (e.g., QuoteManager.reset_for_market). Without
        # this, post-reconnect resting orders are stale and reconcile()
        # skips placement → silent dark periods.
        self._reconnect_callbacks: list[Callable[[list[str]], Awaitable[None]]] = []
        self._load_key()

    def _load_key(self):
        from cryptography.hazmat.primitives import serialization
        if not Path(self.private_key_path).exists():
            raise FileNotFoundError(f"private key not found: {self.private_key_path}")
        with open(self.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _build_auth_headers(self) -> dict:
        """Build KALSHI-ACCESS-* headers signed against path '/trade-api/ws/v2'."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        msg = f"{ts}GET{path}".encode()
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

    # ── Connection ────────────────────────────────────────────────────
    async def connect(self) -> None:
        headers = self._build_auth_headers()
        # websockets library accepts `additional_headers` (v12+) or `extra_headers`
        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**20,
            )
        except TypeError:
            self._ws = await websockets.connect(
                self.url,
                extra_headers=headers,
                ping_interval=20,
                ping_timeout=20,
            )
        _log.info(f"WS connected to {self.url}")

    async def close(self) -> None:
        self._stop = True
        if self._ws:
            await self._ws.close()

    # ── Subscription ──────────────────────────────────────────────────
    async def subscribe_orderbook(self, tickers: list[str]) -> int:
        """Subscribe to orderbook_snapshot + orderbook_delta for a ticker list.
        Returns a Kalshi-assigned subscription id (sid)."""
        self._cmd_id += 1
        cmd = {
            "id":  self._cmd_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }
        await self._ws.send(json.dumps(cmd))
        _log.info(f"subscribed (cmd_id={self._cmd_id}) {len(tickers)} tickers: {tickers[:3]}...")
        # Initialize empty books so callbacks have somewhere to write
        for t in tickers:
            if t not in self.books:
                self.books[t] = BookState(market_ticker=t)
        return self._cmd_id

    def on_update(self, cb: BookCallback) -> None:
        self._callbacks.append(cb)

    def on_reconnect(self, cb: Callable[[list[str]], Awaitable[None]]) -> None:
        """Register a hook fired after successful reconnect+resubscribe.
        Receives the list of resubscribed tickers so consumers can purge
        any per-market in-memory state (e.g., resting orders)."""
        self._reconnect_callbacks.append(cb)

    # ── Message handling ─────────────────────────────────────────────
    @staticmethod
    def _parse_book_side(raw_side) -> list[BookLevel]:
        """Parse levels. Kalshi v2 dollar-fixed-point format:
        [["0.0010", "501.00"], ["0.0020", "100.00"], ...]
        Prices in dollars-with-4-decimals, sizes with 2 decimals.
        We normalize prices to integer cents, sizes to integer contracts."""
        if raw_side is None:
            return []
        out = []
        for lvl in raw_side:
            if isinstance(lvl, (list, tuple)):
                p_raw, s_raw = lvl[0], lvl[1]
            elif isinstance(lvl, dict):
                p_raw = lvl.get("price", 0)
                s_raw = lvl.get("size",  0)
            else:
                continue
            try:
                # price can be str "0.0010" (dollars) or int (cents). Normalize to cents.
                if isinstance(p_raw, str):
                    price_dollars = float(p_raw)
                    price_cents = round(price_dollars * 100)
                elif isinstance(p_raw, float):
                    # Already parsed dollars
                    price_cents = round(p_raw * 100)
                else:
                    price_cents = int(p_raw)
                # size likewise may be str
                size = int(round(float(s_raw) if isinstance(s_raw, str) else s_raw))
            except (TypeError, ValueError):
                continue
            if size > 0 and 0 <= price_cents <= 100:
                out.append(BookLevel(price_cents=price_cents, size=size))
        return out

    def _apply_snapshot(self, book: BookState, msg: dict) -> None:
        """Fully replace the book state from a snapshot message.

        Kalshi's orderbook_snapshot delivers `yes_dollars_fp` (YES bids) and
        `no_dollars_fp` (NO bids). Asks are IMPLIED by the opposite side
        because Kalshi binaries clear at $1: if someone bids NO at $0.25,
        that implies a sell-YES offer at $0.75.
        """
        # Handle both v2 key names and potential legacy names
        yes_side = msg.get("yes_dollars_fp") or msg.get("yes")
        no_side  = msg.get("no_dollars_fp")  or msg.get("no")

        book.yes_bids = sorted(
            self._parse_book_side(yes_side),
            key=lambda l: -l.price_cents,
        )
        book.no_bids = sorted(
            self._parse_book_side(no_side),
            key=lambda l: -l.price_cents,
        )
        # Derive yes_asks from no_bids: if someone offers 25c for no, that's 75c
        # to buy yes (taker). This is how Kalshi orderbooks are structured.
        book.yes_asks = sorted(
            [BookLevel(price_cents=100 - l.price_cents, size=l.size) for l in book.no_bids],
            key=lambda l: l.price_cents,
        )
        book.no_asks = sorted(
            [BookLevel(price_cents=100 - l.price_cents, size=l.size) for l in book.yes_bids],
            key=lambda l: l.price_cents,
        )
        book.snapshot_count += 1
        book.last_update_ts = time.time()

    def _apply_delta(self, book: BookState, msg: dict) -> None:
        """Apply a single delta update: add/update/remove one level.

        Kalshi v2 delta format: { side: "yes"|"no", price: "0.0010"|int,
        delta: int_change_in_size }. Price can be a fixed-point string."""
        side = msg.get("side", "")
        p_raw = msg.get("price", 0)
        # Normalize price to cents
        if isinstance(p_raw, str):
            try:
                price = round(float(p_raw) * 100)
            except ValueError:
                return
        elif isinstance(p_raw, float):
            price = round(p_raw * 100)
        else:
            price = int(p_raw)
        try:
            delta = int(msg.get("delta", 0))
        except (TypeError, ValueError):
            return

        target_list = book.yes_bids if side == "yes" else book.no_bids
        # Find existing level
        for i, lvl in enumerate(target_list):
            if lvl.price_cents == price:
                lvl.size += delta
                if lvl.size <= 0:
                    target_list.pop(i)
                break
        else:
            if delta > 0:
                target_list.append(BookLevel(price_cents=price, size=delta))

        # Keep sorted (desc for bids)
        target_list.sort(key=lambda l: -l.price_cents)

        # Re-derive opposite side (asks from other side's bids)
        book.yes_asks = sorted(
            [BookLevel(price_cents=100 - l.price_cents, size=l.size) for l in book.no_bids],
            key=lambda l: l.price_cents,
        )
        book.no_asks = sorted(
            [BookLevel(price_cents=100 - l.price_cents, size=l.size) for l in book.yes_bids],
            key=lambda l: l.price_cents,
        )

        book.delta_count += 1
        book.last_update_ts = time.time()

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        mtype = msg.get("type", "")
        if mtype == "orderbook_snapshot":
            m = msg.get("msg", {})
            ticker = m.get("market_ticker", "")
            seq = msg.get("seq", 0)
            book = self.books.setdefault(ticker, BookState(market_ticker=ticker))
            self._apply_snapshot(book, m)
            book.last_seq = seq
            for cb in self._callbacks:
                await cb(book)
        elif mtype == "orderbook_delta":
            m = msg.get("msg", {})
            ticker = m.get("market_ticker", "")
            seq = msg.get("seq", 0)
            book = self.books.get(ticker)
            if book is None:
                return
            # Tolerate small seq gaps (compression/coalescing on Kalshi side).
            # Only force re-subscribe on gap > 50 (genuine data loss).
            # Raised from >10 — most gaps are Kalshi coalescing, not real loss,
            # and re-subscribing causes more noise than it fixes.
            if book.last_seq and seq > book.last_seq + 50:
                _log.info(f"seq gap {ticker}: {book.last_seq}→{seq} (gap={seq-book.last_seq-1})")
                await self.subscribe_orderbook([ticker])
                return
            self._apply_delta(book, m)
            book.last_seq = seq
            for cb in self._callbacks:
                await cb(book)
        elif mtype == "subscribed":
            _log.info(f"subscribed ack: {msg}")
        elif mtype == "error":
            _log.error(f"WS error: {msg}")
        # Other message types (trade, ticker, fill) ignored here

    async def run(self) -> None:
        """Main receive loop. Call after connect() + subscribe_orderbook()."""
        assert self._ws is not None, "call connect() first"
        while not self._stop:
            try:
                raw = await self._ws.recv()
                await self._handle_message(raw)
            except websockets.ConnectionClosed as e:
                _log.warning(f"WS closed: {e}; reconnecting in 3s")
                await asyncio.sleep(3)
                try:
                    await self.connect()
                    # Re-subscribe all previously-subscribed tickers
                    tickers = list(self.books.keys())
                    if tickers:
                        await self.subscribe_orderbook(tickers)
                        # 2026-04-22 (Architect audit): notify consumers so
                        # they can purge stale resting state. Without this,
                        # QuoteManager.resting holds pre-disconnect orders
                        # and reconcile() skips placement → dark period.
                        for cb in self._reconnect_callbacks:
                            try:
                                await cb(tickers)
                            except Exception as cb_e:
                                _log.error(f"reconnect callback failed: {cb_e}")
                except Exception as e2:
                    _log.error(f"reconnect failed: {e2}")
                    await asyncio.sleep(5)
            except Exception as e:
                _log.exception(f"unexpected error in WS loop: {e}")
                await asyncio.sleep(1)


# ── Smoke test ────────────────────────────────────────────────────────────
async def _smoke_test():
    """Prove the WS reader works end-to-end against a live market."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from engine.lip_discovery import top_n_to_quote
    top = top_n_to_quote(3)
    tickers = [m["market_ticker"] for m in top]
    print(f"smoke-testing WS on top 3 markets: {tickers}")

    book_updates = {t: 0 for t in tickers}

    async def on_book(book: BookState):
        book_updates[book.market_ticker] = book_updates.get(book.market_ticker, 0) + 1

    ws = KalshiWS()
    await ws.connect()
    ws.on_update(on_book)
    await ws.subscribe_orderbook(tickers)

    # Run for 15 seconds, report
    try:
        await asyncio.wait_for(ws.run(), timeout=15)
    except asyncio.TimeoutError:
        pass

    print()
    print("smoke test results (after 15s):")
    for t, n in book_updates.items():
        book = ws.books.get(t)
        print(f"  {t}: {n} updates  {book}")
    await ws.close()


if __name__ == "__main__":
    asyncio.run(_smoke_test())
