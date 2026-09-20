"""Kalshi WebSocket orderbook reader.

Subscribes to orderbook_snapshot + orderbook_delta channels for a given
list of market tickers. Maintains an in-memory book per market. Handles
sequence-number gaps by requesting a fresh snapshot on mismatch.

Kalshi WS protocol (per docs.kalshi.com/websockets/orderbook-updates):
  - Connect to wss://api.elections.kalshi.com/trade-api/ws/v2
  - Send auth via KALSHI-ACCESS-* headers (RSA-PSS signed on path /trade-api/ws/v2)
  - Send subscribe command with channel + market_tickers
  - Receive `orderbook_snapshot` with full book state:
        msg.yes_dollars_fp / msg.no_dollars_fp = [[price_dollars, size_fp], ...]
  - Receive `orderbook_delta` messages with incremental updates:
        msg.side, msg.price_dollars, msg.delta_fp  (fixed-point strings)
  - Each message carries `sid` (subscription id) and `seq`; seq increments
    by exactly 1 per message *per subscription*, not per market.
  - Gap in seq → the local book has diverged from the venue. It is marked
    `stale`, further deltas are dropped, and we re-subscribe to get a fresh
    snapshot which clears the flag.

2026-09-20 audit #1: the previous reader looked for legacy `price`/`delta`
keys on deltas (v2 sends `price_dollars`/`delta_fp`) so every delta parsed
to price=0/delta=0 and the book silently froze at the snapshot; sizes were
rounded to int (Kalshi allows fractional contracts); and seq gaps of up to
50 were tolerated per-market. All three are fixed here.

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
    size: float  # contracts — Kalshi v2 sends fixed-point, may be fractional


# Sizes below this are treated as an empty level (fixed-point noise guard).
_SIZE_EPS = 1e-9


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
    # Subscription id that delivered the last snapshot. Deltas tagged with a
    # different sid (e.g. from a superseded subscription) are dropped.
    sid: Optional[int] = None
    # True when a seq gap / disconnect was detected and no snapshot has
    # arrived since. A stale book must not be quoted against; consumers
    # must pull quotes. `stale_reason` ∈ {"seq_gap", "disconnect"}.
    stale: bool = False
    stale_since_ts: float = 0.0
    stale_reason: str = ""
    gap_count: int = 0
    # 2026-09-20 review: this venue delivered a price that is not on the
    # whole-cent grid (e.g. $0.4950). We do not represent sub-cent levels
    # and the LIP scorer's DF^(ref − price) exponent is only verified for
    # 1¢ ticks, so such a market is explicitly UNSUPPORTED: the level is
    # never merged into a neighbouring cent, the book is flagged, and
    # consumers must not quote or score it. Cleared by a snapshot whose
    # levels are all on-grid.
    unsupported_grid: bool = False
    off_grid_count: int = 0

    def is_usable(self) -> bool:
        """A book we may quote/score against: has a snapshot, is not stale,
        and every level sits on the supported whole-cent price grid."""
        return self.snapshot_count > 0 and not self.stale and not self.unsupported_grid

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
                f"seq={self.last_seq} snap={self.snapshot_count} delta={self.delta_count}"
                f"{' STALE' if self.stale else ''})")


@dataclass
class FillEvent:
    """One execution against one of our orders (private `fill` channel)."""
    order_id: str
    market_ticker: str
    side: str                       # "yes" | "no" | ""
    count: float                    # contracts filled in this event (fractional ok)
    price_cents_exact: Optional[float]
    is_taker: bool
    trade_id: str                   # execution identity — idempotency key
    ts: float                       # local receive time
    exchange_ts: Optional[float] = None   # venue timestamp (epoch), preserved as sent
    subaccount: str = ""            # subaccount identity, preserved as sent


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
        self._pending_cmd_tickers: dict[int, list[str]] = {}  # cmd id -> tickers (until ack)
        # Sequence tracking is PER SUBSCRIPTION (sid), not per market. Keyed
        # by sid when the message carries one, else by ("ticker", t) fallback.
        self._last_seq: dict = {}
        self._last_resub_ts: dict = {}  # throttle key -> last resubscribe time
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
        # 2026-09-20 review: disconnect callbacks fire the moment the socket
        # drops — BEFORE any reconnect attempt — so consumers can pull
        # resting orders they can no longer manage. Every book is marked
        # stale(reason="disconnect") at the same time.
        self._disconnect_callbacks: list[Callable[[list[str]], Awaitable[None]]] = []
        # Private `fill` channel: consumers keep resting sizes honest between
        # REST resyncs (partial fills).
        self._fill_callbacks: list[Callable[["FillEvent"], Awaitable[None]]] = []
        self.connected: bool = False
        self._grid_warned: set[str] = set()
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
                        salt_length=hashes.SHA256().digest_size),
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
        self.connected = True
        _log.info(f"WS connected to {self.url}")

    async def close(self) -> None:
        self._stop = True
        self.connected = False
        if self._ws:
            await self._ws.close()

    async def subscribe_fills(self) -> int:
        """Subscribe to the private `fill` channel (all markets)."""
        self._cmd_id += 1
        cmd = {"id": self._cmd_id, "cmd": "subscribe", "params": {"channels": ["fill"]}}
        await self._ws.send(json.dumps(cmd))
        self._pending_cmd_tickers[self._cmd_id] = []
        _log.info(f"subscribed fills (cmd_id={self._cmd_id})")
        return self._cmd_id

    def on_fill(self, cb: Callable[["FillEvent"], Awaitable[None]]) -> None:
        self._fill_callbacks.append(cb)

    def on_disconnect(self, cb: Callable[[list[str]], Awaitable[None]]) -> None:
        """Register a hook fired immediately when the socket drops (before
        reconnect). Receives every subscribed ticker."""
        self._disconnect_callbacks.append(cb)

    async def _mark_disconnected(self) -> None:
        """Socket is gone: every local book is now an unverifiable replica."""
        self.connected = False
        now = time.time()
        tickers = list(self.books.keys())
        for b in self.books.values():
            if not b.stale:
                b.stale = True
                b.stale_since_ts = now
            b.stale_reason = "disconnect"
        self._last_seq.clear()
        for cb in self._disconnect_callbacks:
            try:
                await cb(tickers)
            except Exception as e:
                _log.error(f"disconnect callback failed: {e}")

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
                "use_yes_price": False,  # existing parser uses separate YES/NO leg prices
            },
        }
        await self._ws.send(json.dumps(cmd))
        _log.info(f"subscribed (cmd_id={self._cmd_id}) {len(tickers)} tickers: {tickers[:3]}...")
        self._pending_cmd_tickers[self._cmd_id] = list(tickers)
        # Initialize empty books so callbacks have somewhere to write
        for t in tickers:
            if t not in self.books:
                self.books[t] = BookState(market_ticker=t)
        return self._cmd_id

    async def _unsubscribe_sid(self, sid: int) -> None:
        """Drop a superseded subscription so its deltas stop arriving."""
        self._cmd_id += 1
        cmd = {"id": self._cmd_id, "cmd": "unsubscribe", "params": {"sids": [sid]}}
        try:
            await self._ws.send(json.dumps(cmd))
        except Exception as e:  # best effort; deltas on the old sid are dropped anyway
            _log.warning(f"unsubscribe sid={sid} failed: {e}")
        self._sid_to_tickers.pop(sid, None)
        self._last_seq.pop(sid, None)

    def on_update(self, cb: BookCallback) -> None:
        self._callbacks.append(cb)

    def on_reconnect(self, cb: Callable[[list[str]], Awaitable[None]]) -> None:
        """Register a hook fired after successful reconnect+resubscribe.
        Receives the list of resubscribed tickers so consumers can purge
        any per-market in-memory state (e.g., resting orders)."""
        self._reconnect_callbacks.append(cb)

    # ── Message handling ─────────────────────────────────────────────
    @staticmethod
    def _price_to_cents_exact(p_raw) -> Optional[float]:
        """Normalize a price to EXACT cents (may be fractional).

        str   → dollars fixed-point ("0.0010", "0.4950")
        float → dollars (already parsed)
        int   → legacy cents
        Returns None when unparseable or outside [0, 100]."""
        try:
            if isinstance(p_raw, bool):
                return None
            if isinstance(p_raw, str):
                cents = float(p_raw) * 100.0
            elif isinstance(p_raw, float):
                cents = p_raw * 100.0
            elif isinstance(p_raw, int):
                cents = float(p_raw)
            else:
                return None
        except (TypeError, ValueError):
            return None
        if cents != cents or not (0.0 <= cents <= 100.0):
            return None
        return cents

    # Supported price grid: whole cents. Kalshi's standard tick is 1¢; the
    # LIP formula's DF^(ReferencePrice − Price) exponent is verified only in
    # cent units. Anything finer is rejected explicitly (never rounded).
    _GRID_EPS = 1e-6

    @classmethod
    def _price_to_cents(cls, p_raw) -> Optional[int]:
        """Integer cents for an ON-GRID price; None when unparseable, out of
        range, or off the whole-cent grid. Use _price_to_cents_exact to tell
        the last two cases apart."""
        exact = cls._price_to_cents_exact(p_raw)
        if exact is None:
            return None
        nearest = round(exact)
        if abs(exact - nearest) > cls._GRID_EPS:
            return None
        return int(nearest)

    @classmethod
    def _is_off_grid(cls, p_raw) -> bool:
        exact = cls._price_to_cents_exact(p_raw)
        return exact is not None and abs(exact - round(exact)) > cls._GRID_EPS

    @staticmethod
    def _size_to_float(s_raw) -> Optional[float]:
        """Normalize a size (contracts). Fixed-point strings stay fractional —
        Kalshi allows non-integer contract quantities, so rounding here would
        misstate depth and shift the LIP cutoff."""
        try:
            if isinstance(s_raw, bool):
                return None
            v = float(s_raw)
        except (TypeError, ValueError):
            return None
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v

    @classmethod
    def _parse_book_side(cls, raw_side, off_grid: Optional[list] = None) -> list[BookLevel]:
        """Parse levels. Kalshi v2 dollar-fixed-point format:
        [["0.0010", "501.00"], ["0.0020", "100.50"], ...]
        Prices in dollars-with-4-decimals, sizes with 2 decimals.
        Dict levels accept v2 keys (`price_dollars`, `size_fp`) and legacy
        (`price`, `size`). Prices normalize to integer cents; sizes stay float.

        A price that is not on the whole-cent grid is NEVER merged into a
        neighbouring cent (that would collapse distinct levels and shift the
        LIP cutoff). It is dropped and, when `off_grid` is given, appended
        to it so the caller can flag the book as unsupported."""
        if raw_side is None:
            return []
        out = []
        for lvl in raw_side:
            if isinstance(lvl, (list, tuple)):
                if len(lvl) < 2:
                    continue
                p_raw, s_raw = lvl[0], lvl[1]
            elif isinstance(lvl, dict):
                p_raw = lvl.get("price_dollars", lvl.get("price_dollars_fp", lvl.get("price")))
                s_raw = lvl.get("size_fp", lvl.get("size", lvl.get("quantity")))
            else:
                continue
            size = cls._size_to_float(s_raw)
            if size is None:
                continue
            price_cents = cls._price_to_cents(p_raw)
            if price_cents is None:
                if off_grid is not None and cls._is_off_grid(p_raw):
                    off_grid.append(p_raw)
                continue
            if size > _SIZE_EPS:
                out.append(BookLevel(price_cents=price_cents, size=size))
        return out

    def _flag_off_grid(self, book: BookState, samples: list) -> None:
        book.unsupported_grid = True
        book.off_grid_count += len(samples)
        if book.market_ticker not in self._grid_warned:
            self._grid_warned.add(book.market_ticker)
            _log.warning(f"UNSUPPORTED PRICE GRID {book.market_ticker}: off-cent "
                         f"prices {samples[:3]} — market will not be quoted or scored")

    @staticmethod
    def _derive_asks(book: BookState) -> None:
        """Asks are implied by the opposite side's bids (binaries clear at $1):
        a NO bid at 25¢ is a sell-YES offer at 75¢."""
        book.yes_asks = sorted(
            [BookLevel(price_cents=100 - l.price_cents, size=l.size) for l in book.no_bids],
            key=lambda l: l.price_cents,
        )
        book.no_asks = sorted(
            [BookLevel(price_cents=100 - l.price_cents, size=l.size) for l in book.yes_bids],
            key=lambda l: l.price_cents,
        )

    def _apply_snapshot(self, book: BookState, msg: dict) -> None:
        """Fully replace the book state from a snapshot message.

        Kalshi's orderbook_snapshot delivers `yes_dollars_fp` (YES bids) and
        `no_dollars_fp` (NO bids). Asks are IMPLIED by the opposite side
        because Kalshi binaries clear at $1: if someone bids NO at $0.25,
        that implies a sell-YES offer at $0.75.
        """
        # v2 key names first; legacy names as fallback. Use explicit None
        # checks — an empty list is a valid (empty) side, not "missing".
        yes_side = msg.get("yes_dollars_fp")
        if yes_side is None:
            yes_side = msg.get("yes_dollars", msg.get("yes"))
        no_side = msg.get("no_dollars_fp")
        if no_side is None:
            no_side = msg.get("no_dollars", msg.get("no"))

        off: list = []
        book.yes_bids = sorted(self._parse_book_side(yes_side, off), key=lambda l: -l.price_cents)
        book.no_bids  = sorted(self._parse_book_side(no_side, off),  key=lambda l: -l.price_cents)
        self._derive_asks(book)
        book.snapshot_count += 1
        book.last_update_ts = time.time()
        # A snapshot is authoritative: whatever divergence we had is gone.
        book.stale = False
        book.stale_since_ts = 0.0
        book.stale_reason = ""
        # Grid support is re-evaluated from the full snapshot.
        book.unsupported_grid = False
        if off:
            self._flag_off_grid(book, off)

    def _apply_delta(self, book: BookState, msg: dict) -> bool:
        """Apply a single delta update: add/update/remove one level.

        Kalshi v2 delta format:
            { market_ticker, side: "yes"|"no", price_dollars: "0.4900",
              delta_fp: "-12.50" }
        Legacy `price` / `delta` keys are accepted as a fallback. Returns
        False when the message could not be parsed (nothing applied)."""
        side = msg.get("side", "")
        if side not in ("yes", "no"):
            return False
        p_raw = msg.get("price_dollars", msg.get("price_dollars_fp", msg.get("price")))
        d_raw = msg.get("delta_fp", msg.get("delta"))
        delta = self._size_to_float(d_raw)
        if delta is None:
            return False
        price = self._price_to_cents(p_raw)
        if price is None:
            if self._is_off_grid(p_raw):
                # Explicit rejection: do NOT fold $0.4950 into the 50¢ level.
                self._flag_off_grid(book, [p_raw])
            return False

        target_list = book.yes_bids if side == "yes" else book.no_bids
        for i, lvl in enumerate(target_list):
            if lvl.price_cents == price:
                lvl.size += delta
                if lvl.size <= _SIZE_EPS:
                    target_list.pop(i)
                break
        else:
            if delta > _SIZE_EPS:
                target_list.append(BookLevel(price_cents=price, size=delta))

        target_list.sort(key=lambda l: -l.price_cents)   # desc for bids
        self._derive_asks(book)
        book.delta_count += 1
        book.last_update_ts = time.time()
        return True

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        mtype = msg.get("type", "")
        if mtype == "orderbook_snapshot":
            m = msg.get("msg", {}) or {}
            ticker = m.get("market_ticker", "")
            sid = msg.get("sid")
            seq = msg.get("seq")
            book = self.books.setdefault(ticker, BookState(market_ticker=ticker))
            # A snapshot re-bases the stream: record seq, never flag a gap.
            self._seq_reset(sid, ticker, seq)
            if sid is not None:
                self._sid_to_tickers.setdefault(sid, [])
                if ticker not in self._sid_to_tickers[sid]:
                    self._sid_to_tickers[sid].append(ticker)
            self._apply_snapshot(book, m)
            book.sid = sid if sid is not None else book.sid
            book.last_seq = int(seq) if seq is not None else 0
            for cb in self._callbacks:
                await cb(book)
        elif mtype == "orderbook_delta":
            m = msg.get("msg", {}) or {}
            ticker = m.get("market_ticker", "")
            sid = msg.get("sid")
            seq = msg.get("seq")
            book = self.books.get(ticker)
            if book is None:
                return
            # Deltas from a superseded subscription (we resubscribed after a
            # gap) must not be applied on top of the new snapshot.
            if sid is not None and book.sid is not None and sid != book.sid:
                return
            verdict = self._seq_check(sid, ticker, seq)
            if verdict == "dup":
                return
            if verdict == "gap":
                await self._on_seq_gap(sid, ticker, seq)
                return
            if book.stale:
                # Diverged and waiting for a snapshot; applying would only
                # compound the divergence.
                return
            grid_ok_before = not book.unsupported_grid
            if self._apply_delta(book, m):
                book.last_seq = int(seq) if seq is not None else book.last_seq
                for cb in self._callbacks:
                    await cb(book)
            elif book.unsupported_grid and grid_ok_before:
                # Transition to unsupported: consumers must pull quotes now.
                book.last_seq = int(seq) if seq is not None else book.last_seq
                for cb in self._callbacks:
                    await cb(book)
            elif not book.unsupported_grid:
                _log.warning(f"unparseable delta {ticker}: {m}")
        elif mtype == "fill":
            m = msg.get("msg", {}) or {}
            ev = self._parse_fill(m)
            if ev is None:
                _log.debug(f"unparseable fill: {m}")
                return
            for cb in self._fill_callbacks:
                try:
                    await cb(ev)
                except Exception as e:
                    _log.error(f"fill callback failed for {ev.order_id}: {e}")
        elif mtype == "subscribed":
            m = msg.get("msg", {}) or {}
            sid = m.get("sid")
            cmd_id = msg.get("id")
            tickers = self._pending_cmd_tickers.pop(cmd_id, None)
            if sid is not None and tickers is not None:
                self._sid_to_tickers[sid] = list(tickers)
            _log.info(f"subscribed ack: sid={sid} cmd_id={cmd_id} n={len(tickers or [])}")
        elif mtype == "error":
            _log.error(f"WS error: {msg}")
        # Other message types (trade, ticker, fill) ignored here

    @classmethod
    def _parse_fill(cls, m: dict) -> Optional["FillEvent"]:
        """Private `fill` channel payload → FillEvent. Accepts v2 fixed-point
        keys (count_fp, *_price_dollars) with legacy fallbacks."""
        order_id = m.get("order_id") or ""
        ticker = m.get("market_ticker") or m.get("ticker") or ""
        count = cls._size_to_float(m.get("count_fp", m.get("count")))
        if not order_id or not ticker or count is None or count <= 0:
            return None
        side = str(m.get("side", "")).lower()
        if side == "yes":
            p_raw = m.get("yes_price_dollars", m.get("yes_price_fp", m.get("yes_price")))
        elif side == "no":
            p_raw = m.get("no_price_dollars", m.get("no_price_fp", m.get("no_price")))
        else:
            p_raw = None
        price_exact = cls._price_to_cents_exact(p_raw) if p_raw is not None else None
        # The documented fill payload may supply only YES price even for NO.
        if price_exact is None and side == "no":
            yes_raw = m.get("yes_price_dollars", m.get("yes_price_fp", m.get("yes_price")))
            yes_price = cls._price_to_cents_exact(yes_raw) if yes_raw is not None else None
            if yes_price is not None:
                price_exact = 100.0 - yes_price
        # Exchange timestamp: epoch seconds/millis (int/float/str) or ISO.
        exchange_ts: Optional[float] = None
        raw_ts = m.get("ts", m.get("created_time"))
        if raw_ts is not None:
            try:
                v = float(raw_ts)
                exchange_ts = v / 1000.0 if v > 1e11 else v
            except (TypeError, ValueError):
                try:
                    exchange_ts = datetime.fromisoformat(
                        str(raw_ts).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    exchange_ts = None
        sub = m.get("subaccount_id", m.get("subaccount", ""))
        return FillEvent(
            order_id=order_id,
            market_ticker=ticker,
            side=side,
            count=float(count),
            price_cents_exact=price_exact,
            is_taker=bool(m.get("is_taker", False)),
            trade_id=str(m.get("trade_id", "") or ""),
            ts=time.time(),
            exchange_ts=exchange_ts,
            subaccount=str(sub) if sub is not None else "",
        )

    # ── Sequence tracking (per subscription) ─────────────────────────
    @staticmethod
    def _seq_key(sid, ticker: str):
        return sid if sid is not None else ("ticker", ticker)

    def _seq_reset(self, sid, ticker: str, seq) -> None:
        if seq is None:
            return
        try:
            self._last_seq[self._seq_key(sid, ticker)] = int(seq)
        except (TypeError, ValueError):
            pass

    def _seq_check(self, sid, ticker: str, seq) -> str:
        """Returns "ok", "dup" (seq already seen → drop) or "gap"."""
        if seq is None:
            return "ok"
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return "ok"
        key = self._seq_key(sid, ticker)
        last = self._last_seq.get(key)
        if last is None:
            self._last_seq[key] = seq
            return "ok"
        if seq <= last:
            return "dup"
        tol = int(getattr(settings, "WS_SEQ_GAP_TOLERANCE", 0) or 0)
        self._last_seq[key] = seq
        if seq > last + 1 + tol:
            return "gap"
        return "ok"

    async def _on_seq_gap(self, sid, ticker: str, seq) -> None:
        """A delta was lost. Every book under this subscription has diverged:
        flag them stale (consumers pull quotes), notify, and re-subscribe
        (throttled) so a fresh snapshot arrives and clears the flag."""
        if sid is not None and self._sid_to_tickers.get(sid):
            affected = list(self._sid_to_tickers[sid])
        else:
            affected = [ticker]
        now = time.time()
        newly = []
        for t in affected:
            b = self.books.get(t)
            if b is None:
                continue
            b.gap_count += 1
            if not b.stale:
                b.stale = True
                b.stale_since_ts = now
                newly.append(b)
        _log.warning(f"seq gap sid={sid} at seq={seq} ({ticker}): "
                     f"{len(affected)} book(s) marked stale")
        for b in newly:
            for cb in self._callbacks:
                try:
                    await cb(b)
                except Exception as e:
                    _log.error(f"stale callback failed for {b.market_ticker}: {e}")
        key = self._seq_key(sid, ticker)
        if now - self._last_resub_ts.get(key, 0.0) < 10:
            return  # throttle: a resubscribe is already in flight
        self._last_resub_ts[key] = now
        if sid is not None:
            await self._unsubscribe_sid(sid)
        await self.subscribe_orderbook(affected)

    async def run(self) -> None:
        """Main receive loop. Call after connect() + subscribe_orderbook()."""
        assert self._ws is not None, "call connect() first"
        while not self._stop:
            try:
                raw = await self._ws.recv()
                await self._handle_message(raw)
            except websockets.ConnectionClosed as e:
                _log.warning(f"WS closed: {e}; reconnecting in 3s")
                # 2026-09-20 review: pull exposure FIRST. Books are stale
                # from this instant and our resting orders are unmanaged.
                await self._mark_disconnected()
                await asyncio.sleep(3)
                try:
                    await self.connect()
                    # Re-subscribe all previously-subscribed tickers
                    tickers = list(self.books.keys())
                    if tickers:
                        await self.subscribe_orderbook(tickers)
                    if self._fill_callbacks:
                        await self.subscribe_fills()
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
