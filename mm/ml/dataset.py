"""Point-in-time fill-toxicity dataset builder (offline, paper only).

    python -m mm.ml.dataset build  [--rec-dir DIR] [--out-dir DIR] [--full]
    python -m mm.ml.dataset report [--out-dir DIR] [--json PATH]

Samples (one row each, ``source`` says where it came from):

* ``live``        paper fills of the running engine (journal written by
                  mm.ml.harvest). ``synthetic`` is the engine's own flag (PM US
                  inferred prints / polled-book cross fills).
* ``replay``      paper fills from replaying the recordings through RunLoop
                  (mm.ml.replayfills journal). Same fill model, not the live run.
* ``quote_bg``    resting replay quotes sampled once a minute; label
                  ``filled_60s`` (fill-probability background / negatives).
* ``trade_proxy`` every recorded public print, from the resting maker's side:
                  the maker bought ``side`` at the print price. Not our fills;
                  a proxy population with the same features/labels, used while
                  real fills are scarce. Never counted by the readiness gate.

Time and leakage
----------------
Frames are re-ordered into EVENT time (exchange timestamp when the frame
carries one: delta ``msg.ts_ms``, trade ``trade.ts_ms``, snapshot
``exchange_ts``; else the recorder's receive ``ts``) with a 30 s re-order
window. A sample has a ``cutoff`` event time; its features are computed from
the state built from frames with event time < cutoff, before any frame at or
after cutoff is applied. Each row carries ``feat_max_ts`` (newest event time
that touched the features) and the builder asserts feat_max_ts < cutoff.
Labels at horizon h use the state as of cutoff + h (settlement value if the
market settled first).

* trade_proxy / replay fills: cutoff = the print's exchange time.
* live fills: the journal has the engine's receive time; the builder matches
  the recorded print (same market, receive ts within 0.5 s, taker on the
  opposite side) and uses its exchange time, else cutoff = fill ts - 15 s
  (``cutoff_mode=conservative``).
* PM US synthetic prints are inferred from the difference of two polls; the
  poll that produced the print is excluded (features use the previous poll).

Incremental: frame files are processed in order once each (all but the newest,
which is still being written); the file after is read as label look-ahead
only. Book/history state is checkpointed after each file. Pure stdlib.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import gzip
import heapq
import json
import math
import os
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path

REC_DIR = "/var/lib/lip-maker/recordings"
OUT_DIR = "/var/lib/lip-maker/ml"
HORIZONS = (("1m", 60.0), ("5m", 300.0), ("30m", 1800.0))
TOXIC_HORIZON = "5m"
TOXIC_CENTS = float(os.environ.get("LIP_ML_TOXIC_CENTS", 2.0))   # toxic: 5m markout < -2c
REORDER_S = 30.0
LIVE_MATCH_S = 0.5
LIVE_CONSERVATIVE_S = 15.0
HIST_KEEP_S = 900.0
TRADE_KEEP_S = 300.0
FAST_MOVE_CENTS = 5.0          # engine policy LIP_PULL_MOVE_CENTS=5
READY_MIN_FILLS = 300
READY_MIN_DAYS = 7
MAX_OUT_BYTES = int(float(os.environ.get("LIP_ML_MAX_GB", 2.0)) * 1e9)


# ---------------------------------------------------------------- parsing
def _f(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _cents(x):
    v = _f(x)
    return None if v is None else round(v * 100.0, 4)


def frame_kind(fr: dict) -> str:
    return str(fr.get("kind") or fr.get("type") or "")


def frame_venue(fr: dict) -> str:
    if fr.get("venue"):
        return str(fr["venue"])
    m = market_of(fr) or ""
    return "pmus" if m.startswith("PMUS:") else "kalshi"


def market_of(fr: dict):
    k = frame_kind(fr)
    if k in ("orderbook_snapshot", "orderbook_delta"):
        return (fr.get("msg") or {}).get("market_ticker")
    if k == "trade":
        t = fr.get("trade") or {}
        return t.get("market_ticker") or t.get("ticker")
    return fr.get("market")


def event_ts(fr: dict):
    """Exchange-side time of a frame when it has one, else receive ts."""
    k = frame_kind(fr)
    recv = _f(fr.get("ts"))
    if fr.get("venue") == "pmus" or fr.get("synthetic"):
        return recv
    if k == "orderbook_delta":
        ms = _f((fr.get("msg") or {}).get("ts_ms"))
        if ms:
            return ms / 1000.0
    elif k == "trade":
        t = fr.get("trade") or {}
        ms = _f(t.get("ts_ms"))
        if ms:
            return ms / 1000.0
        if _f(t.get("ts")):
            return _f(t.get("ts"))
    elif k == "orderbook_snapshot":
        if _f(fr.get("exchange_ts")):
            return _f(fr.get("exchange_ts"))
    return recv


def trade_fields(fr: dict):
    """(market, maker_side, maker_price_cents, count, synthetic) of a print.
    taker_side 'no' (taker bought NO) hits YES bids: maker bought YES at the
    yes price; taker 'yes' hits NO bids: maker bought NO at the no price."""
    t = fr.get("trade") or {}
    m = t.get("market_ticker") or t.get("ticker")
    taker = str(t.get("taker_side") or "").lower()
    yes_c = _cents(t.get("yes_price_dollars"))
    if yes_c is None and _f(t.get("yes_price")) is not None:
        yes_c = _f(t.get("yes_price"))
    no_c = _cents(t.get("no_price_dollars"))
    if no_c is None and yes_c is not None:
        no_c = 100.0 - yes_c
    cnt = _f(t.get("count_fp"), None) or _f(t.get("count"), 0.0)
    if taker == "no":
        side, px = "yes", yes_c
    elif taker == "yes":
        side, px = "no", no_c
    else:
        return None
    if not m or px is None or not cnt:
        return None
    return m, side, px, cnt, bool(fr.get("synthetic") or t.get("synthetic"))


# ---------------------------------------------------------------- state
class Book:
    __slots__ = ("yes", "no", "ts", "prev")

    def __init__(self):
        self.yes: dict = {}
        self.no: dict = {}
        self.ts = None
        self.prev = None            # PM US: (yes, no, ts) of the previous poll

    def best(self, side):
        lv = self.yes if side == "yes" else self.no
        return max(lv) if lv else None


class State:
    """Everything the features may read. Built in event-time order only."""

    def __init__(self):
        self.books: dict = {}
        self.hist: dict = {}         # market -> deque[(ts, yes_best, no_best)]
        self.trades: dict = {}       # market -> deque[(ts, count, taker_side)]
        self.programs: dict = {}     # market -> program row
        self.settled: dict = {}      # market -> (ts, result)
        self.max_ts = None           # newest event ts applied
        self.touched: dict = {}      # market -> newest event ts applied to it

    def apply(self, fr: dict, ets: float) -> None:
        k = frame_kind(fr)
        m = market_of(fr)
        if k == "program":
            if m:
                self.programs[m] = {x: fr.get(x) for x in PROGRAM_KEYS}
            return
        if k == "settlement":
            if m and m not in self.settled and fr.get("result") in ("yes", "no"):
                self.settled[m] = (ets, fr["result"])
                self._touch(m, ets)
            return
        if not m or ets is None:
            return
        if k == "orderbook_snapshot":
            msg = fr.get("msg") or {}
            b = self.books.get(m)
            if b is None:
                b = self.books[m] = Book()
            if frame_venue(fr) == "pmus":
                b.prev = (dict(b.yes), dict(b.no), b.ts)
            b.yes = {p: s for p, s in ((_cents(a), _f(c, 0.0)) for a, c in msg.get("yes_dollars_fp") or [])
                     if p is not None and s > 0}
            b.no = {p: s for p, s in ((_cents(a), _f(c, 0.0)) for a, c in msg.get("no_dollars_fp") or [])
                    if p is not None and s > 0}
            b.ts = ets
            self._hist(m, b, ets)
        elif k == "orderbook_delta":
            b = self.books.get(m)
            if b is None:
                return               # no snapshot base yet: book unknown
            msg = fr.get("msg") or {}
            p = _cents(msg.get("price_dollars"))
            d = _f(msg.get("delta_fp"), 0.0)
            lv = b.yes if msg.get("side") == "yes" else b.no
            if p is None:
                return
            s = lv.get(p, 0.0) + d
            if s > 1e-9:
                lv[p] = s
            else:
                lv.pop(p, None)
            b.ts = ets
            self._hist(m, b, ets)
        elif k == "trade":
            t = fr.get("trade") or {}
            cnt = _f(t.get("count_fp"), None) or _f(t.get("count"), 0.0)
            dq = self.trades.setdefault(m, collections.deque())
            dq.append((ets, cnt, str(t.get("taker_side") or "").lower()))
            while dq and dq[0][0] < ets - TRADE_KEEP_S:
                dq.popleft()
            # prints are read with a strict ts < cutoff filter (features), so a
            # same-millisecond print of a sweep does not mark the market touched
            self.max_ts = ets if self.max_ts is None else max(self.max_ts, ets)
            return
        else:
            return
        self._touch(m, ets)

    def _touch(self, m, ets):
        self.touched[m] = max(ets, self.touched.get(m, ets))
        self.max_ts = ets if self.max_ts is None else max(self.max_ts, ets)

    def _hist(self, m, b, ets):
        dq = self.hist.setdefault(m, collections.deque())
        yb, nb = b.best("yes"), b.best("no")
        if not dq or dq[-1][1] != yb or dq[-1][2] != nb:
            dq.append((ets, yb, nb))
        while len(dq) > 2 and dq[1][0] < ets - HIST_KEEP_S:
            dq.popleft()


PROGRAM_KEYS = ("series", "category", "venue", "event_ticker", "period_reward_usd", "period_seconds",
                "discount_factor", "target_size", "start_ts", "end_ts", "close_ts", "occurrence_ts",
                "rank_score", "fee_type", "pm_period", "max_spread_usd")


def _yes_mid(yb, nb):
    if yb is None or nb is None:
        return None
    return (yb + (100.0 - nb)) / 2.0


def _side_val(yes_mid, side):
    if yes_mid is None:
        return None
    return yes_mid if side == "yes" else 100.0 - yes_mid


def _hist_at(dq, t):
    """(yes_best, no_best) as of event time t (last entry with ts <= t)."""
    if not dq:
        return None
    i = bisect.bisect_right([x[0] for x in dq], t) - 1
    return None if i < 0 else dq[i]


def _depth(levels: dict, best, within):
    if best is None:
        return 0.0
    return sum(s for p, s in levels.items() if p >= best - within)


# ---------------------------------------------------------------- features
FEATURES_NUM = (
    "price_c", "side_mid_c", "dist_mid_c", "behind_best_c", "spread_c",
    "own_top_sz", "opp_top_sz", "imb_top", "own_d5", "opp_d5", "imb_d5",
    "mid_move_5s", "mid_move_30s", "mid_move_5m", "opp_move_30s", "opp_move_5m",
    "trades_60s", "vol_60s", "trades_5m", "vol_5m", "hit_vol_5m", "secs_since_trade",
    "book_age_s", "quote_age_s", "hours_to_close", "hours_to_period_end", "hours_to_event",
    "inventory_side", "period_reward_usd", "reward_rate_usd_h", "target_size", "discount_factor",
    "rank_score", "hour_utc", "dow", "fast_move_30s", "fast_move_quote", "count",
)
FEATURES_CAT = ("venue", "side", "category", "series")


def features(state: State, *, market: str, side: str, price: float, cutoff: float,
             count: float = 0.0, quote_ts=None, quote_best0=None, inventory=None,
             use_prev_poll: bool = False) -> dict:
    """Point-in-time features from ``state`` (which must hold only frames with
    event time < cutoff)."""
    opp = "no" if side == "yes" else "yes"
    b = state.books.get(market)
    yes_lv = no_lv = {}
    book_ts = None
    if b is not None:
        if use_prev_poll and b.prev is not None and b.ts is not None and b.ts >= cutoff - 3.0:
            # the newest poll produced this (synthetic) print: use the one before
            yes_lv, no_lv, book_ts = b.prev
            if book_ts is None:
                yes_lv, no_lv = {}, {}
        else:
            yes_lv, no_lv, book_ts = b.yes, b.no, b.ts
    own_lv, opp_lv = (yes_lv, no_lv) if side == "yes" else (no_lv, yes_lv)
    own_best = max(own_lv) if own_lv else None
    opp_best = max(opp_lv) if opp_lv else None
    yes_b = max(yes_lv) if yes_lv else None
    no_b = max(no_lv) if no_lv else None
    side_mid = _side_val(_yes_mid(yes_b, no_b), side)
    row = {"market": market, "side": side, "cutoff": cutoff, "price_c": price, "count": count}
    row["side_mid_c"] = side_mid
    row["dist_mid_c"] = None if side_mid is None else side_mid - price
    row["behind_best_c"] = None if own_best is None else own_best - price
    row["spread_c"] = None if (yes_b is None or no_b is None) else (100.0 - no_b) - yes_b
    ot = own_lv.get(own_best, 0.0) if own_best is not None else 0.0
    pt = opp_lv.get(opp_best, 0.0) if opp_best is not None else 0.0
    row["own_top_sz"], row["opp_top_sz"] = ot, pt
    row["imb_top"] = (ot - pt) / (ot + pt) if ot + pt > 0 else None
    od, pd = _depth(own_lv, own_best, 5.0), _depth(opp_lv, opp_best, 5.0)
    row["own_d5"], row["opp_d5"] = od, pd
    row["imb_d5"] = (od - pd) / (od + pd) if od + pd > 0 else None
    dq = state.hist.get(market)
    feat_ts = [book_ts] if book_ts is not None else []
    if use_prev_poll and dq and b is not None and book_ts is not None and book_ts != b.ts:
        # the newest hist entry belongs to the excluded poll
        dq = collections.deque(x for x in dq if book_ts is None or x[0] <= book_ts)
    for name, lag in (("5s", 5.0), ("30s", 30.0), ("5m", 300.0)):
        h = _hist_at(dq, cutoff - lag) if dq else None
        then = _side_val(_yes_mid(h[1], h[2]), side) if h else None
        row[f"mid_move_{name}"] = None if (then is None or side_mid is None) else side_mid - then
        if name in ("30s", "5m"):
            ob_then = (h[2] if side == "yes" else h[1]) if h else None
            row[f"opp_move_{name}"] = None if (ob_then is None or opp_best is None) else opp_best - ob_then
    row["fast_move_30s"] = (1.0 if (row["opp_move_30s"] or 0) >= FAST_MOVE_CENTS else 0.0) \
        if row["opp_move_30s"] is not None else None
    if quote_best0 is not None and opp_best is not None:
        o0 = quote_best0[1] if side == "yes" else quote_best0[0]
        row["fast_move_quote"] = None if o0 is None else (1.0 if opp_best - o0 >= FAST_MOVE_CENTS else 0.0)
    else:
        row["fast_move_quote"] = None
    tq = [x for x in state.trades.get(market, ()) if x[0] < cutoff]
    row["trades_60s"] = float(sum(1 for x in tq if x[0] >= cutoff - 60))
    row["vol_60s"] = sum(x[1] for x in tq if x[0] >= cutoff - 60)
    row["trades_5m"] = float(len([x for x in tq if x[0] >= cutoff - 300]))
    row["vol_5m"] = sum(x[1] for x in tq if x[0] >= cutoff - 300)
    row["hit_vol_5m"] = sum(x[1] for x in tq if x[0] >= cutoff - 300 and x[2] == opp)
    row["secs_since_trade"] = (cutoff - tq[-1][0]) if tq else None
    if tq:
        feat_ts.append(tq[-1][0])
    row["book_age_s"] = None if book_ts is None else cutoff - book_ts
    row["quote_age_s"] = None if quote_ts is None else cutoff - float(quote_ts)
    row["inventory_side"] = inventory
    pg = state.programs.get(market) or {}
    for k in ("series", "category", "event_ticker", "fee_type"):
        row[k] = pg.get(k)
    row["venue"] = "pmus" if market.startswith("PMUS:") else "kalshi"
    if not row["series"]:
        row["series"] = market.split("-")[0]
    for k in ("period_reward_usd", "target_size", "discount_factor", "rank_score"):
        row[k] = _f(pg.get(k))
    ps = _f(pg.get("period_seconds"))
    row["reward_rate_usd_h"] = (row["period_reward_usd"] / ps * 3600.0) if (ps and row["period_reward_usd"] is not None) else None
    for k, src in (("hours_to_close", "close_ts"), ("hours_to_period_end", "end_ts"), ("hours_to_event", "occurrence_ts")):
        v = _f(pg.get(src))
        row[k] = None if v is None else (v - cutoff) / 3600.0
    dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    row["hour_utc"] = dt.hour + dt.minute / 60.0
    row["dow"] = float(dt.weekday())
    row["day"] = dt.strftime("%Y-%m-%d")
    row["feat_max_ts"] = max(feat_ts) if feat_ts else None
    return row


def label_value(state: State, market: str, side: str, due: float):
    """Side value (cents) as of event time ``due`` and its staleness (s)."""
    st = state.settled.get(market)
    if st is not None and st[0] <= due:
        return (100.0 if st[1] == side else 0.0), 0.0
    b = state.books.get(market)
    if b is None:
        return None, None
    v = _side_val(_yes_mid(b.best("yes"), b.best("no")), side)
    if v is None:
        return None, None
    return v, (due - b.ts) if b.ts is not None else None


# ---------------------------------------------------------------- builder
INF = float("inf")


class Builder:
    """Event-time sample builder. Call ``feed(frame, emit)`` in receive order,
    then ``finish()``. ``emit=False`` frames (label look-ahead) only update
    state; they never create samples."""

    def __init__(self, state: State | None = None, *, live_fills=(), replay_rows=(),
                 proxy: bool = True):
        self.state = state or State()
        self.proxy = proxy
        self.buf: list = []            # (event_ts, prio, seq, frame, emit)
        self.seq = 0
        self.req: list = []            # (cutoff, seq, meta)
        self.due: list = []            # (due_ts, seq, row, horizon)
        self.rows: list = []
        self.open: dict = {}           # id(row) -> [row, labels left]
        self.stats = collections.Counter()
        t0 = self.state.max_ts if self.state.max_ts is not None else -INF
        t0 -= 2 * REORDER_S
        # journal rows at/before the checkpoint were handled by an earlier file
        self.live = sorted((r for r in live_fills if float(r["ts"]) > t0), key=lambda r: float(r["ts"]))
        self.replay = sorted((r for r in replay_rows if float(r["cutoff"]) > t0), key=lambda r: float(r["cutoff"]))
        self.live_i = self.replay_i = 0
        self.live_pending: list = []
        self.recv_max = None
        self.seen_trades: set = set()

    # ----------------------------------------------------- receive order
    def feed(self, fr: dict, emit: bool = True) -> None:
        k = frame_kind(fr)
        if k in ("ws_raw", "screen", "shard", "disconnect", "reconnect"):
            return
        if k == "program":
            self.state.apply(fr, None)   # reward params are known when recorded
            return
        recv = _f(fr.get("ts"))
        ets = event_ts(fr)
        if ets is None:
            return
        if recv is not None:
            self.recv_max = recv if self.recv_max is None else max(self.recv_max, recv)
            if emit:
                self.schedule_journals(recv + REORDER_S)
                if k == "trade":
                    self._match_live(fr, recv, ets)
        self.seq += 1
        # at equal event time a print sorts before book frames: Kalshi stamps the
        # delta the print caused with the print's own ts_ms (and sends it first)
        heapq.heappush(self.buf, (ets, 0 if k == "trade" else 1, self.seq, fr, emit))
        if self.recv_max is not None:
            self.drain(self.recv_max - REORDER_S)

    def schedule_journals(self, upto: float, final: bool = False) -> None:
        """Turn journal rows with time <= ``upto`` into sample requests."""
        while self.replay_i < len(self.replay) and float(self.replay[self.replay_i]["cutoff"]) <= upto:
            r = self.replay[self.replay_i]
            self.replay_i += 1
            self._request(float(r["cutoff"]), dict(r))
        live_upto = upto - REORDER_S + 1.0 if not final else upto
        while self.live_i < len(self.live) and float(self.live[self.live_i]["ts"]) <= live_upto:
            self.live_pending.append(self.live[self.live_i])
            self.live_i += 1
        keep = []
        for lf in self.live_pending:
            if final or (self.recv_max is not None and self.recv_max > float(lf["ts"]) + 2.0):
                meta = self._live_meta(lf)
                meta["cutoff_mode"] = "conservative"
                self._request(float(lf["ts"]) - LIVE_CONSERVATIVE_S, meta)
            else:
                keep.append(lf)
        self.live_pending = keep

    def _live_meta(self, lf):
        return {"source": "live", "market": lf["market"], "side": lf["side"],
                "price": float(lf["price_cents"]), "count": float(lf.get("count") or 0),
                "synthetic": bool(lf.get("synthetic")), "fill_ts": float(lf["ts"]),
                "inventory": lf.get("inventory_side")}

    def _match_live(self, fr, recv, ets):
        if not self.live_pending:
            return
        tf = trade_fields(fr)
        if tf is None:
            return
        m, side, _px, _c, _s = tf
        for lf in self.live_pending:
            if lf["market"] == m and lf["side"] == side and abs(float(lf["ts"]) - recv) <= LIVE_MATCH_S:
                self.live_pending.remove(lf)
                meta = self._live_meta(lf)
                meta["cutoff_mode"] = "matched_print"
                meta["prev_poll"] = bool(fr.get("synthetic"))
                self._request(ets, meta)
                return

    def _request(self, cutoff: float, meta: dict) -> None:
        if self.state.touched.get(meta["market"], -INF) >= cutoff:
            self.stats["late_request"] += 1
            return
        self.seq += 1
        heapq.heappush(self.req, (cutoff, self.seq, meta))

    # ----------------------------------------------------- event order
    def drain(self, watermark: float) -> None:
        """Apply buffered frames with event time < watermark, in event order."""
        while self.buf and self.buf[0][0] < watermark:
            ets, _p, _s, fr, emit = heapq.heappop(self.buf)
            self._advance(ets)
            if emit and self.proxy and frame_kind(fr) == "trade":
                tf = trade_fields(fr)
                tid = (fr.get("trade") or {}).get("trade_id")
                if tf is not None and tid not in self.seen_trades:
                    self.seen_trades.add(tid)
                    m, side, px, cnt, syn = tf
                    self._sample(ets, {"source": "trade_proxy", "market": m, "side": side, "price": px,
                                       "count": cnt, "synthetic": syn, "prev_poll": syn,
                                       "cutoff_mode": "print"})
            self.state.apply(fr, ets)

    def _advance(self, ets: float) -> None:
        """Before applying a frame at event time ``ets``: emit requests with
        cutoff <= ets (every applied frame is then strictly older than the
        cutoff) and resolve labels due strictly before ets (every frame at or
        before the due time is applied)."""
        while True:
            nd = self.due[0][0] if self.due else INF
            nr = self.req[0][0] if self.req else INF
            if nr <= ets and nr <= nd:
                cutoff, _s, meta = heapq.heappop(self.req)
                self._sample(cutoff, meta)
            elif nd < ets:
                _d, _s, row, hname = heapq.heappop(self.due)
                self._label(row, hname, _d)
            else:
                return

    def finish(self) -> None:
        self.schedule_journals(self.recv_max if self.recv_max is not None else -INF, final=True)
        self.drain(INF)
        mx = self.state.max_ts if self.state.max_ts is not None else -INF
        while self.due and self.due[0][0] <= mx:
            _d, _s, row, hname = heapq.heappop(self.due)
            self._label(row, hname, _d)
        self.stats["unlabeled_horizons"] += len(self.due)
        self.due = []
        self.stats["requests_beyond_data"] += len(self.req)
        self.req = []
        for row, _n in list(self.open.values()):
            self._close(row)

    def _sample(self, cutoff: float, meta: dict) -> None:
        # leakage guard: nothing about this market at/after the cutoff applied yet
        if self.state.touched.get(meta["market"], -INF) >= cutoff:
            self.stats["late_request"] += 1
            return
        row = features(self.state, market=meta["market"], side=meta["side"], price=float(meta["price"]),
                       cutoff=cutoff, count=float(meta.get("count") or 0.0), quote_ts=meta.get("quote_ts"),
                       quote_best0=meta.get("quote_best0"), inventory=meta.get("inventory"),
                       use_prev_poll=bool(meta.get("prev_poll")))
        fmt = row.get("feat_max_ts")
        if fmt is not None and fmt >= cutoff:
            raise AssertionError(f"feature leakage: feat_max_ts {fmt} >= cutoff {cutoff}")
        for k in ("source", "synthetic", "cutoff_mode", "fill_ts", "jid", "filled_60s"):
            if k in meta:
                row[k] = meta[k]
        row["synthetic"] = bool(row.get("synthetic"))
        if row["side_mid_c"] is None and meta["source"] in ("trade_proxy", "quote_bg"):
            self.stats[f"{meta['source']}_no_book"] += 1
            return
        self.stats[f"sample_{meta['source']}"] += 1
        for hname, secs in HORIZONS:
            row[f"markout_c_{hname}"] = None
            row[f"markout_usd_{hname}"] = None
            self.seq += 1
            heapq.heappush(self.due, (cutoff + secs, self.seq, row, hname))
        self.open[id(row)] = [row, len(HORIZONS)]

    def _label(self, row, hname, due) -> None:
        v, stale = label_value(self.state, row["market"], row["side"], due)
        if v is not None:
            mc = v - float(row["price_c"])
            row[f"markout_c_{hname}"] = round(mc, 4)
            row[f"markout_usd_{hname}"] = round(mc * float(row.get("count") or 0.0) / 100.0, 6)
            row[f"label_stale_s_{hname}"] = None if stale is None else round(stale, 3)
        ent = self.open.get(id(row))
        if ent is not None:
            ent[1] -= 1
            if ent[1] <= 0:
                self._close(row)

    def _close(self, row) -> None:
        if self.open.pop(id(row), None) is None:
            return
        m5 = row.get(f"markout_c_{TOXIC_HORIZON}")
        row["toxic"] = None if m5 is None else int(m5 < -TOXIC_CENTS)
        row["toxic_threshold_c"] = TOXIC_CENTS
        self.rows.append(row)


def build_stream(frames, lookahead=(), *, state=None, live_fills=(), replay_rows=(), proxy=True):
    """Samples for ``frames`` (receive order) with ``lookahead`` frames used for
    labels only. Returns (rows, builder, state_snapshot_bytes_at_end_of_frames)."""
    b = Builder(state, live_fills=live_fills, replay_rows=replay_rows, proxy=proxy)
    for fr in frames:
        b.feed(fr, emit=True)
    end = b.recv_max
    b.schedule_journals(end if end is not None else -INF, final=True)
    b.drain(INF)                      # every frame of this window is applied
    snap = pickle.dumps(b.state, protocol=pickle.HIGHEST_PROTOCOL)
    stop = (end or 0) + max(s for _n, s in HORIZONS) + REORDER_S + 5
    for fr in lookahead:
        b.feed(fr, emit=False)
        if b.recv_max is not None and b.recv_max > stop:
            break
    b.finish()
    return b.rows, b, snap


# ---------------------------------------------------------------- IO
def read_frames(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    return
    except (EOFError, OSError):
        return


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    op = gzip.open if p.name.endswith(".gz") else open
    out = []
    with op(p, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def list_recordings(rec_dir):
    d = Path(rec_dir)
    return sorted(p for p in d.glob("frames-*.jsonl.gz")) if d.is_dir() else []


def _round(row):
    return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in row.items()}


def build(rec_dir=REC_DIR, out_dir=OUT_DIR, *, full=False, max_files=None, log=print) -> dict:
    """Process every closed, not yet processed recording file (incremental)."""
    out = Path(out_dir)
    (out / "samples").mkdir(parents=True, exist_ok=True)
    (out / "state").mkdir(parents=True, exist_ok=True)
    manifest_p = out / "state" / "manifest.json"
    ckpt_p = out / "state" / "feature_state.pkl"
    manifest = {"done": []}
    if not full and manifest_p.exists():
        manifest = json.loads(manifest_p.read_text())
    state = None
    if not full and ckpt_p.exists() and manifest["done"]:
        with ckpt_p.open("rb") as fh:
            state = pickle.load(fh)
    files = list_recordings(rec_dir)
    done = set(manifest["done"])
    # the newest file is still being written; the one before needs it as look-ahead
    todo = [p for p in files[:-1] if p.name not in done]
    if max_files:
        todo = todo[: int(max_files)]
    names = [p.name for p in files]
    if todo and manifest["done"] and manifest["done"][-1] in names \
            and names.index(todo[0].name) != names.index(manifest["done"][-1]) + 1:
        log(f"gap before {todo[0].name}: feature state reset")
        state = None
    live = read_jsonl(out / "fills_live.jsonl")
    replay = read_jsonl(out / "replay_journal.jsonl")
    tot = collections.Counter()
    t0 = time.time()
    for p in todo:
        nxt = files[names.index(p.name) + 1]
        rows, b, snap = build_stream(read_frames(p), read_frames(nxt), state=state,
                                     live_fills=live, replay_rows=replay)
        dst = out / "samples" / p.name.replace("frames-", "samples-")
        tmp = dst.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(_round(r), separators=(",", ":")) + "\n")
        os.replace(tmp, dst)
        state = pickle.loads(snap)
        ckpt_tmp = ckpt_p.with_suffix(".tmp")
        ckpt_tmp.write_bytes(snap)
        os.replace(ckpt_tmp, ckpt_p)
        manifest["done"].append(p.name)
        manifest_p.write_text(json.dumps(manifest))
        tot.update(b.stats)
        log(f"{p.name}: {len(rows)} rows {dict(b.stats)}")
    prune(out)
    return {"files_processed": len(todo), "files_done_total": len(manifest["done"]),
            "wall_s": round(time.time() - t0, 1), "stats": dict(tot)}


def prune(out: Path) -> list:
    files = sorted((out / "samples").glob("samples-*.jsonl.gz"))
    total = sum(p.stat().st_size for p in files)
    gone = []
    for p in files:
        if total <= MAX_OUT_BYTES:
            break
        total -= p.stat().st_size
        p.unlink()
        gone.append(p.name)
    return gone


def load_samples(out_dir=OUT_DIR) -> list:
    rows = []
    for p in sorted((Path(out_dir) / "samples").glob("samples-*.jsonl.gz")):
        rows.extend(read_jsonl(p))
    return rows


# ---------------------------------------------------------------- readiness
def readiness(rows: list, *, min_fills=READY_MIN_FILLS, min_days=READY_MIN_DAYS, live_journal=None) -> dict:
    def summ(sel):
        lab = [r for r in sel if r.get("toxic") is not None]
        by_v = collections.Counter(r.get("venue") for r in sel)
        by_c = collections.Counter(f"{r.get('venue')}/{r.get('category')}" for r in sel)
        return {"n": len(sel), "labeled": len(lab), "toxic": sum(r["toxic"] for r in lab),
                "days": len({r.get("day") for r in sel}), "by_venue": dict(by_v),
                "by_category": dict(by_c.most_common(25))}
    groups = {
        "live_real": [r for r in rows if r.get("source") == "live" and not r.get("synthetic")],
        "live_synthetic": [r for r in rows if r.get("source") == "live" and r.get("synthetic")],
        "replay_real": [r for r in rows if r.get("source") == "replay" and not r.get("synthetic")],
        "replay_synthetic": [r for r in rows if r.get("source") == "replay" and r.get("synthetic")],
        "quote_bg": [r for r in rows if r.get("source") == "quote_bg"],
        "trade_proxy": [r for r in rows if r.get("source") == "trade_proxy" and not r.get("synthetic")],
        "trade_proxy_synthetic": [r for r in rows if r.get("source") == "trade_proxy" and r.get("synthetic")],
    }
    out = {k: summ(v) for k, v in groups.items()}
    real = out["live_real"]
    ready = real["labeled"] >= min_fills and real["days"] >= min_days
    rep = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paper_only": True,
        "gate": {"min_real_fills": min_fills, "min_days": min_days,
                 "counted": "live paper fills, non-synthetic, with a 5m label"},
        "status": "READY" if ready else "NOT_READY",
        "reason": None if ready else (f"{real['labeled']}/{min_fills} labeled real fills, "
                                      f"{real['days']}/{min_days} days"),
        "toxic_definition": f"5m markout < -{TOXIC_CENTS:g}c (side mid at t+5m minus fill price)",
        "groups": out,
    }
    if live_journal is not None:
        rep["live_journal_fills"] = live_journal
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mm.ml.dataset")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--rec-dir", default=REC_DIR)
    b.add_argument("--out-dir", default=OUT_DIR)
    b.add_argument("--full", action="store_true")
    b.add_argument("--max-files", type=int)
    r = sub.add_parser("report")
    r.add_argument("--out-dir", default=OUT_DIR)
    r.add_argument("--json")
    r.add_argument("--min-fills", type=int, default=READY_MIN_FILLS)
    r.add_argument("--min-days", type=int, default=READY_MIN_DAYS)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        res = build(a.rec_dir, a.out_dir, full=a.full, max_files=a.max_files)
        print(json.dumps(res))
        return 0
    rows = load_samples(a.out_dir)
    lj = read_jsonl(Path(a.out_dir) / "fills_live.jsonl")
    rep = readiness(rows, min_fills=a.min_fills, min_days=a.min_days,
                    live_journal={"n": len(lj), "synthetic": sum(1 for x in lj if x.get("synthetic"))})
    txt = json.dumps(rep, indent=1)
    if a.json:
        tmp = Path(a.json).with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(txt)
        os.replace(tmp, a.json)
    g = rep["groups"]
    print(f"readiness: {rep['status']}" + (f" ({rep['reason']})" if rep["reason"] else ""))
    for k, v in g.items():
        print(f"  {k:24s} n={v['n']:7d} labeled={v['labeled']:7d} toxic={v['toxic']:6d} days={v['days']} venues={v['by_venue']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
