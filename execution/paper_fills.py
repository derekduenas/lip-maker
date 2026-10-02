"""Causal fill simulation for paper orders, driven by observed public trades.

The gap this closes
-------------------
The operating loop placed PAPER orders that could never fill. A paper
session therefore reported quotes and reward estimates but no inventory, no
trading P&L and no exit activity — the half of the economics that decides
whether the strategy makes money. research/maker_replay.py had a fill model,
but it runs offline over a recorded timeline and the runner never imports it.

The model
---------
Causal, and deliberately pessimistic. We only fill when a trade we actually
observed would have reached us:

1. QUEUE. When our order is placed we are LAST in the queue at our price.
   Queue-ahead is the depth already resting at exactly our price, taken from
   the book snapshot at placement. We never assume priority we did not earn.

2. LATENCY. The order is not eligible for any trade timestamped before
   `activation_ts` (placement + latency). A trade that happened while our
   order was in flight is not ours.

3. CONSUMPTION. A trade counts against our bid when the taker was hitting
   our side and the print is AT our price or THROUGH it (a lower price on
   that side). Buying YES rests as a YES bid, and it is consumed by a
   taker buying NO. A print that stops one cent short of our bid is not a
   fill. A print through the bid means the level was swept: the queue
   ahead of us is cleared, then the print's size fills us. Ignoring those
   prints leaves the adverse-selection fills out of the paper result.

4. ORDERING. At our price, observed volume first exhausts the queue ahead
   of us; only the remainder fills us, and never more than our remaining
   size. A through-print zeroes that queue first (the level could not
   trade through while size was still ahead), then fills min(print, remaining).
   A quantity smaller than 1e-9 is float residue and is dropped.

Every fill carries the trade_id that caused it, so a fill can be traced back
to a real, public, timestamped market event rather than to a coin flip.
A trade tagged ``synthetic`` (PM US: inferred from two book polls, see
mm/unattended/pmus_paper.synth_trades) is not a public print; its fills
carry ``synthetic: True`` so reports can label them low fidelity.

What it still cannot know
-------------------------
Real queue position (cancellations ahead of us are invisible, so we
overstate the queue and under-fill AT our price), hidden/iceberg size,
and whether our own order would have changed the taker's behaviour.
A through-print is not under-filled: the level was swept.
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

_log = logging.getLogger(__name__)

# Public trade history. Callers pass a base, or this reads settings so a
# demo session is not stuck on the production host hardcoded here before.
API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_LATENCY_MS = 250.0
# Float residue from fixed-point counts. A 1e-15 "fill" is not a contract.
DUST_QTY = 1e-9
# De-duplication memory for trade ids. Trades arrive roughly in time order,
# so the most recent ids are the only ones a redelivery can repeat; the set
# used to grow for the life of the process.
SEEN_TRADES_MAX = 200_000


def api_base() -> str:
    try:
        from config import settings
        configured = getattr(settings, "KALSHI_API_BASE", "") or API_BASE
    except Exception:
        configured = API_BASE
    return str(configured).rstrip("/")


def trades_url(query: str, base: str | None = None) -> str:
    root = (base or api_base()).rstrip("/")
    return f"{root}/markets/trades?{query}"


def _ctx() -> ssl.SSLContext:
    try:
        if ssl.get_default_verify_paths().cafile:
            return ssl.create_default_context()
    except Exception:
        pass
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _cents(v) -> int:
    return int(round(float(v) * 100))


def _side_cents(tr: dict, side: str) -> int:
    """Trade price for ``side`` in whole cents, 0 when absent/unparseable.

    ``{side}_price_dollars`` / ``{side}_price_fp`` are dollars; the legacy
    ``{side}_price`` is integer cents (an int, or an integer string). A
    missing NO price is 100 - YES (as kalshi_ws._parse_fill does)."""
    for key in (f"{side}_price_dollars", f"{side}_price_fp"):
        raw = tr.get(key)
        if raw not in (None, ""):
            try:
                return _cents(raw)
            except (TypeError, ValueError):
                return 0
    raw = tr.get(f"{side}_price")
    if raw is not None and not isinstance(raw, bool):
        try:
            if isinstance(raw, int):
                return int(raw)
            txt = str(raw).strip()
            return _cents(txt) if (isinstance(raw, float) or "." in txt) else int(txt)
        except (TypeError, ValueError):
            return 0
    if side == "no":
        yes = _side_cents(tr, "yes")
        return 100 - yes if 0 < yes < 100 else 0
    return 0


class _SeenIds:
    """Fixed-size LRU set of trade ids."""

    def __init__(self, maxlen: int) -> None:
        self.maxlen = max(1, int(maxlen))
        self._d: OrderedDict = OrderedDict()

    def __contains__(self, key) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)

    def add(self, key) -> None:
        self._d[key] = None
        self._d.move_to_end(key)
        while len(self._d) > self.maxlen:
            self._d.popitem(last=False)


@dataclass
class SimOrder:
    """One resting paper order being tracked for fills."""
    order_id: str
    market_ticker: str
    side: str                 # "yes" | "no"
    price_cents: int
    remaining: float
    queue_ahead: float
    activation_ts: float
    program_id: str = ""
    filled: float = 0.0
    fill_trade_ids: list = field(default_factory=list)


class PaperFillSimulator:
    """Tracks paper orders and fills them from observed public trades."""

    def __init__(self, *, latency_ms: float = DEFAULT_LATENCY_MS,
                 capture_path: Optional[str] = None,
                 seen_trades_max: int = SEEN_TRADES_MAX):
        self.latency_sec = latency_ms / 1000.0
        self.orders: dict[str, SimOrder] = {}
        self._ctx = _ctx()
        self.seen_trades_max = int(seen_trades_max)
        self._seen_trades = _SeenIds(self.seen_trades_max)
        self._capture = capture_path
        self.trades_observed = 0
        self.fills_generated = 0

    # ── order tracking ────────────────────────────────────────────────
    def track(self, *, order_id: str, market_ticker: str, side: str,
              price_cents: int, size: float, book, program_id: str = "",
              now: Optional[float] = None) -> SimOrder:
        now = time.time() if now is None else now
        levels = (book.yes_bids if side == "yes" else book.no_bids) if book else []
        queue_ahead = sum(float(l.size) for l in levels
                          if int(l.price_cents) == int(price_cents))
        o = SimOrder(order_id=order_id, market_ticker=market_ticker, side=side,
                     price_cents=int(price_cents), remaining=float(size),
                     queue_ahead=queue_ahead,
                     activation_ts=now + self.latency_sec,
                     program_id=program_id)
        self.orders[order_id] = o
        return o

    def untrack(self, order_id: str) -> None:
        self.orders.pop(order_id, None)

    # ── trade ingestion ───────────────────────────────────────────────
    def fetch_trades(self, tickers: Iterable[str], *, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        for t in tickers:
            q = urllib.parse.urlencode({"ticker": t, "limit": limit})
            try:
                req = urllib.request.Request(trades_url(q),
                                             headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20, context=self._ctx) as r:
                    out.extend(json.loads(r.read()).get("trades", []))
            except Exception as e:
                _log.debug(f"trade fetch failed {t}: {e}")
        return out

    def _record(self, obj: dict) -> None:
        if not self._capture:
            return
        try:
            with open(self._capture, "a") as fh:
                fh.write(json.dumps(obj) + "\n")
        except Exception:
            pass

    def apply_trades(self, trades: list[dict]) -> list[dict]:
        """Consume queue and emit fills. Returns fill dicts."""
        fills: list[dict] = []
        for tr in sorted(trades, key=lambda t: t.get("created_time", "")):
            tid = tr.get("trade_id")
            if not tid or tid in self._seen_trades:
                continue
            self._seen_trades.add(tid)
            self.trades_observed += 1
            self._record({"kind": "trade", "trade": tr})
            t_ts = _ts(tr.get("created_time", ""))
            ticker = tr.get("ticker")
            try:
                qty = float(tr.get("count_fp") or tr.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if qty < DUST_QTY:
                continue
            yes_c = _side_cents(tr, "yes")
            no_c = _side_cents(tr, "no")
            taker = (tr.get("taker_side") or "").lower()

            for o in list(self.orders.values()):
                if o.market_ticker != ticker or o.remaining < DUST_QTY:
                    continue
                if t_ts < o.activation_ts:
                    continue        # in flight when this trade happened
                # Our YES bid is hit by a taker buying NO at or through our
                # price; our NO bid is hit by a taker buying YES.
                if o.side == "yes":
                    if taker != "no" or yes_c <= 0 or yes_c > o.price_cents:
                        continue
                    through = yes_c < o.price_cents
                else:
                    if taker != "yes" or no_c <= 0 or no_c > o.price_cents:
                        continue
                    through = no_c < o.price_cents
                if through:
                    # The print is past our level, so the queue at our
                    # price was cleared before this trade could happen.
                    o.queue_ahead = 0.0
                vol = qty
                if o.queue_ahead > 0:
                    used = min(o.queue_ahead, vol)
                    o.queue_ahead -= used
                    vol -= used
                if vol < DUST_QTY:
                    continue
                got = min(vol, o.remaining)
                if got < DUST_QTY:
                    continue
                o.remaining -= got
                if o.remaining < DUST_QTY:
                    o.remaining = 0.0
                o.filled += got
                o.fill_trade_ids.append(tid)
                self.fills_generated += 1
                fill = {"order_id": o.order_id, "market_ticker": ticker,
                        "side": o.side, "price_cents": o.price_cents,
                        "count": got, "trade_id": tid, "ts": t_ts,
                        "program_id": o.program_id}
                if tr.get("synthetic"):
                    # Inferred from book polls (PM US), not a public trade:
                    # downstream reports label this fill low fidelity.
                    fill["synthetic"] = True
                self._record({"kind": "paper_fill", "fill": fill})
                fills.append(fill)
                if o.remaining <= 0:
                    self.orders.pop(o.order_id, None)
        return fills
