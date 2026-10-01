"""Continuous paper/demo run: select, size, quote, score, allocate, risk.

``run_recorded`` reads a websocket JSONL file and does not open a socket.
``--run`` without a replay file opens the demo websocket when a key file
is present. Production hosts are refused. Live arming stays off.

Paper (``LIP_PAPER=true``, the default) simulates fills with
``PaperFillSimulator``. Demo (``LIP_DEMO=true`` and paper off) builds
post-only orders and sends them only to a demo host. Paper wins when
both flags are set, so the droplet unit cannot send.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests

from engine.lip_accrual import RestingOrder, SecondAccrual
from engine.lip_calibration import RatioObs, series_factors
from engine.lip_reconcile import (
    EstimateRow, credits_from_ledger, infer_reward_credits, reconcile,
)
from engine.lip_scorer import ProgramParams
from execution.paper_fills import PaperFillSimulator
from mm.compound import MarketSample, reallocate
from mm.ops import skew_is_excessive
from mm.risk import FillClock, Limits, RiskEngine
from mm.selector import KalshiMarket, allocate
from mm.session_gates import (
    clamp_contracts, inside_close_window, pull_before_close_s, single_fill_cap_usd,
)
from mm.unattended.feed import DEMO_WS_URL
from mm.unattended.optimize import optimize_sizes
from mm.unattended.service import UnattendedRefused
from mm.venues.kalshi_rest import DEMO_HOSTS, PRODUCTION_HOSTS

SELECT_EVERY_S = 600.0


def _flag(env: dict, name: str, default: str) -> bool:
    return str(env.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def resolve_mode(environ: dict | None = None) -> str:
    """Paper, else demo, else refuse. Live arming flags are not consulted."""
    env = os.environ if environ is None else environ
    if _flag(env, "LIP_PAPER", "true"):
        return "paper"
    if _flag(env, "LIP_DEMO", "false"):
        return "demo"
    raise UnattendedRefused("live trading is not armed")


def resolve_ws_url(raw: str | None = None) -> str:
    """Demo host unless ``LIP_KALSHI_WS_URL`` names another non-production URL."""
    url = raw if raw is not None else (os.environ.get("LIP_KALSHI_WS_URL") or DEMO_WS_URL)
    host = (urlsplit(url).hostname or "").lower()
    if host in PRODUCTION_HOSTS or "api.elections.kalshi.com" in url:
        raise UnattendedRefused(f"production host refused: {host or url}")
    return url


def assert_demo_host(ws_url: str) -> str:
    host = (urlsplit(ws_url).hostname or "").lower()
    if host not in DEMO_HOSTS:
        raise UnattendedRefused(f"demo orders require a demo host, got {host or ws_url}")
    return host


class DemoPoster:
    """Post-only order to a demo host. The sender is the only I/O."""

    def __init__(self, host: str, sender: Callable[[dict], dict]) -> None:
        if host not in DEMO_HOSTS:
            raise UnattendedRefused(f"demo orders require a demo host, got {host}")
        from mm.venues.readonly import reject_market_data_reader
        reject_market_data_reader(sender)
        self.host = host
        self.sender = sender

    def place(self, *, market: str, side: str, price_cents: int, size: float,
              opposing_bid_cents: int) -> dict:
        from execution.order_request import build_limit_order, to_event_order_v2
        legacy = build_limit_order(
            ticker=market, side=side, price_cents=int(price_cents),
            size_contracts=max(1, int(size)),
            client_order_id=f"lip-{market}-{side}"[:64],
            best_opposing_bid_cents=int(opposing_bid_cents),
            time_in_force="good_till_canceled",
        )
        body = to_event_order_v2(legacy)
        if body.get("post_only") is not True:
            raise UnattendedRefused("demo order missing post_only")
        sent = self.sender(body)
        if isinstance(sent, dict):
            sent.setdefault("post_only", True)
            sent.setdefault("host", self.host)
        return sent


def socket_plan(ws_url: str, *, key_path: str | None = None) -> dict:
    """Whether ``--run`` may open a socket. A missing key does not connect."""
    url = resolve_ws_url(ws_url)
    host = assert_demo_host(url)
    path = key_path if key_path is not None else (
        os.environ.get("KALSHI_PRIVATE_KEY_PATH") or "")
    if not path or not Path(path).is_file():
        return {"socket": False, "stage": "waiting_for_demo_key", "url": url, "host": host}
    return {"socket": True, "stage": "connect", "url": url, "host": host}


def _bids(levels) -> list[tuple[int, float]]:
    return [(int(level.price_cents), float(level.size)) for level in levels]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _passive_cents(price_cents: int, opposing_bid_cents: int) -> int:
    """One tick inside the opposing touch so a demo post-only order can rest."""
    implied = 100 - int(opposing_bid_cents)
    price = int(price_cents)
    if price >= implied:
        price = implied - 1
    return price


@dataclass
class _Program:
    market: str
    series: str
    period_reward_usd: float
    period_seconds: float
    discount_factor: float
    target_size: float
    start_ts: float
    end_ts: float
    close_ts: float | None
    days_to_settle: float | None
    exchange_index: int | None
    shard_cash_usd: float
    category: str | None = None
    days_from_close: bool = False
    rank_score: float | None = None
    rank_penalty_per_day: float = 0.0
    occurrence_ts: float | None = None
    event_ticker: str | None = None
    # Patch 21: venue abstraction (kalshi | pmus) and per-venue rule inputs.
    venue: str = "kalshi"
    max_spread_usd: float | None = None
    sports_single: bool = False
    fee_type: str = "quadratic"
    fee_multiplier: float = 1.0
    program_id: str = ""


VENUES = ("kalshi", "pmus")
# Bounded history for the long-running live loop (counts stay exact).
LIST_CAP = 20000


def rank_live_score(net_per_day: float, penalty_per_day: float, capital_usd: float) -> float:
    """Final allocation rank: (plan net $/day - markout penalty $/day) / $ capital."""
    if not capital_usd:
        return 0.0
    return (float(net_per_day) - float(penalty_per_day)) / float(capital_usd)


class RunLoop:
    """One pass over recorded or live frames. The constructor opens nothing."""

    def __init__(self, *, mode: str = "paper", bankroll: float = 10_000.0,
                 select_every: float = SELECT_EVERY_S,
                 chunk: float = 100.0,
                 poster: DemoPoster | None = None,
                 series_stats: dict | None = None,
                 latency_ms: float = 250.0,
                 pull_before_s: float | None = None,
                 fill_cap: float | None = None,
                 first_select_warmup_s: float = 0.0,
                 books_ready_fraction: float = 0.8,
                 carry_forward: bool = False) -> None:
        if mode not in ("paper", "demo"):
            raise UnattendedRefused("live trading is not armed")
        self.mode = mode
        self.bankroll = float(bankroll)
        self.select_every = float(select_every)
        self.chunk = float(chunk)
        self.poster = poster
        self.series_stats = series_stats or {}
        self.pull_before_s = pull_before_close_s() if pull_before_s is None else float(pull_before_s)
        self.fill_cap = single_fill_cap_usd() if fill_cap is None else float(fill_cap)
        self.sim = PaperFillSimulator(latency_ms=latency_ms)
        self.risk = RiskEngine(
            limits=Limits.from_capital(Decimal(str(self.bankroll))),
            clock=FillClock(),
        )
        self.programs: dict[str, _Program] = {}
        self.accruals: dict[str, SecondAccrual] = {}
        self.open_seconds: dict[str, int | None] = {}
        self.resting: dict[str, dict] = {}
        self.committed: dict[str, Decimal] = {}
        self.quotes: list[dict] = []
        self.cancels: list[dict] = []
        self.fills: list[dict] = []
        self.risk_rows: list[dict] = []
        self.excluded: list[tuple[str, str]] = []
        self.ledger: list[dict] = []
        self.cash: dict | None = None
        self.selection_count = 0
        self.last_select_ts: float | None = None
        self.last_plan: dict = {}
        self.first_select_warmup_s = float(first_select_warmup_s)
        self.books_ready_fraction = float(books_ready_fraction)
        self._first_frame_ts: float | None = None
        self.screen_stats: dict = {}
        # Live path: score quiet seconds of a usable book where we rest a
        # quote (the book is known; no message means no change).
        self.carry_forward = bool(carry_forward)
        self._last_closed: int | None = None
        self._compact_at: float = 0.0
        self.cap_skips: list[dict] = []
        self.alloc_budget_usd: float | None = None
        self.kill = None
        self.now = 0.0
        self.socket_opened = False
        # Patch 15: inventory, cooldowns, fill markouts, re-peg bookkeeping.
        self.position: dict[str, dict] = {}
        self.cooldown: dict[tuple, float] = {}
        self.fill_marks: list[dict] = []
        self.policy_skips: list[tuple[str, str]] = []
        self.pulls: dict[str, int] = {}
        self.repegs_n = 0
        self._repeg_at: dict[str, float] = {}
        # Patch 16: external fair value (defensive). Set by service when enabled.
        self.fv = None
        self._fv_wanted: set = set()
        self._fv_state: dict = {}
        self.fv_blocks: dict = {}
        # Patch 18: inventory skew counters.
        self.skew_stats: dict = {}
        # Patch 21: external venue frames (PM US poller thread -> this loop).
        # Only the loop's own thread mutates loop state: the poller only
        # put()s frames; drain_external() runs on the frame thread.
        import queue as _queue
        self.ext_queue = _queue.SimpleQueue()
        self.ext_frames_n = 0
        self._book_ts: dict[str, float] = {}
        self.resting_view: frozenset = frozenset()
        self.quoted_ever: set = set()
        self.quotes_total = 0
        self.cancels_total = 0
        self.pm_rebate_usd = 0.0
        self.closed_periods: dict[str, float] = {}
        self.refeeds_n = 0

    def add_program(self, row: dict) -> None:
        market = str(row["market"])
        old_prog = self.programs.get(market)
        old_acc = self.accruals.get(market)
        start = float(row.get("start_ts") or 0)
        period = float(row.get("period_seconds") or 86400)
        end = float(row["end_ts"]) if row.get("end_ts") is not None else start + period
        close = None if row.get("close_ts") is None else float(row["close_ts"])
        days = None if row.get("days_to_settle") is None else float(row["days_to_settle"])
        shard = row.get("exchange_index")
        prog = _Program(
            market=market,
            series=str(row.get("series") or market.split("-", 1)[0]),
            period_reward_usd=float(row.get("period_reward_usd") or 0),
            period_seconds=period,
            discount_factor=float(row.get("discount_factor") or 0.5),
            target_size=float(row.get("target_size") or 100),
            start_ts=start,
            end_ts=end,
            close_ts=close,
            days_to_settle=days,
            exchange_index=None if shard is None else int(shard),
            shard_cash_usd=float(row.get("shard_cash_usd") or 1e9),
            category=row.get("category"),
            days_from_close=bool(row.get("days_from_close")),
            rank_score=None if row.get("rank_score") is None else float(row["rank_score"]),
            rank_penalty_per_day=float(row.get("rank_penalty_per_day") or 0.0),
            occurrence_ts=None if row.get("occurrence_ts") is None else float(row["occurrence_ts"]),
            event_ticker=row.get("event_ticker") or None,
            venue=str(row.get("venue") or "kalshi"),
            max_spread_usd=None if row.get("max_spread_usd") is None else float(row["max_spread_usd"]),
            sports_single=bool(row.get("sports_single")),
            fee_type=str(row.get("fee_type") or "quadratic"),
            fee_multiplier=float(row.get("fee_multiplier") if row.get("fee_multiplier") is not None else 1.0),
            program_id=str(row.get("program_id") or market),
        )
        if prog.venue not in VENUES:
            raise ValueError(f"unknown venue {prog.venue!r}")
        if old_prog is not None and old_acc is not None and (
                old_prog.program_id, old_prog.start_ts, old_prog.end_ts, old_prog.period_reward_usd,
                old_prog.target_size, old_prog.discount_factor, old_prog.max_spread_usd) == (
                prog.program_id, prog.start_ts, prog.end_ts, prog.period_reward_usd,
                prog.target_size, prog.discount_factor, prog.max_spread_usd):
            # Same program window re-fed (e.g. refreshed metadata): keep the
            # accrual and book, update the descriptive fields only.
            self.programs[market] = prog
            return
        self.programs[market] = prog
        params = ProgramParams(
            market_ticker=market,
            target_size=prog.target_size,
            discount_factor=prog.discount_factor,
            period_reward_usd=prog.period_reward_usd,
            program_id=prog.program_id,
            period_seconds=prog.period_seconds,
            start_ts=prog.start_ts,
            end_ts=prog.end_ts,
            rules="pmus" if prog.venue == "pmus" else "kalshi",
            max_spread_usd=prog.max_spread_usd if prog.venue == "pmus" else None,
        )
        acc = SecondAccrual(params, series=prog.series)
        if old_acc is not None:
            # Patch 21: a new program window for a known market (period
            # roll-over). Keep the live book (the WS sends a snapshot only on
            # subscribe) and our resting orders; archive the old raw accrual.
            acc.book = old_acc.book
            acc.resting = list(old_acc.resting)
            try:
                self.closed_periods[market] = self.closed_periods.get(market, 0.0) + float(old_acc.raw_usd())
            except Exception:
                pass
            self.refeeds_n += 1
        self.accruals[market] = acc
        self.open_seconds.setdefault(market, None)

    def on_frame(self, row: dict) -> None:
        kind = str(row.get("kind") or row.get("type") or "")
        if kind == "program":
            self.add_program(row)
            return
        if kind == "screen":
            self.screen_stats = dict(row.get("stats") or {})
            return
        if kind == "shard":
            prog = self.programs.get(str(row.get("market") or ""))
            if prog is not None and row.get("exchange_index") is not None:
                prog.exchange_index = int(row["exchange_index"])
            return
        if kind == "credit":
            entry = dict(row)
            tagged = str(entry.get("entry_kind") or entry.get("reward_kind") or "")
            if tagged:
                entry["kind"] = tagged
            self.ledger.append(entry)
            return
        if kind == "cash":
            self.cash = row
            return
        ts = float(row.get("ts") if row.get("ts") is not None else self.now)
        self.now = ts
        if kind in ("orderbook_snapshot", "orderbook_delta"):
            self._close_elapsed(int(ts))
            self._on_book(row, ts)
            self._maybe_select(ts)
            self._pull(ts)
            return
        if kind == "trade":
            self._close_elapsed(int(ts))
            self._on_trade(row, ts)
            self._maybe_select(ts)
            self._pull(ts)
            return
        if kind == "clock":
            self._close_elapsed(int(ts))
            self._maybe_select(ts)
            self._pull(ts)

    def _close_elapsed(self, second: int) -> None:
        if self.carry_forward:
            if self._last_closed is not None and second <= self._last_closed:
                return
            self._last_closed = second
        for market, accrual in self.accruals.items():
            quoted = market in self.resting
            idle = self.carry_forward and not quoted
            open_s = self.open_seconds.get(market)
            if open_s is not None and open_s < second:
                nxt = accrual._next
                if nxt is None or open_s >= nxt:
                    if idle:
                        # Not quoting: nothing to earn; do not score (CPU) and
                        # do not report the second as unknown.
                        accrual.omit_until(open_s + 1, status="idle")
                    else:
                        accrual.score_second(open_s)
                self.open_seconds[market] = None
            if (self.carry_forward and market in self.resting and accrual._next is not None
                    and 0 < second - accrual._next <= CARRY_FORWARD_MAX_S
                    and accrual.book.book.is_usable() and self._book_fresh(market, second)):
                while accrual._next < second:
                    accrual.score_second(accrual._next)
            accrual.omit_until(second, status="idle" if idle else "missed")
        if self.carry_forward and second - self._compact_at >= 300:
            self._compact_at = second
            for accrual in self.accruals.values():
                accrual.compact(keep=120)

    def _on_book(self, row: dict, ts: float) -> None:
        body = row.get("msg") or {}
        market = str(body.get("market_ticker") or "")
        accrual = self.accruals.get(market)
        if accrual is None:
            return
        exchange_ts = row.get("exchange_ts")
        if exchange_ts is not None and skew_is_excessive(ts, float(exchange_ts)):
            accrual.book.note_disconnect()
            return
        accrual.on_message(row, ts)
        self.open_seconds[market] = int(ts)
        self._book_ts[market] = ts

    # ------------------------------------------------------------ patch 21
    def _venue(self, market: str) -> str:
        prog = self.programs.get(market)
        return prog.venue if prog is not None else "kalshi"

    def _book_fresh(self, market: str, ts: float) -> bool:
        """Kalshi books are WS-maintained (quiet = unchanged). PM US books are
        polled snapshots: a book older than LIP_PMUS_STALE_S (30 s) is not
        carried forward or quoted against."""
        if self._venue(market) != "pmus":
            return True
        seen = self._book_ts.get(market)
        return seen is not None and ts - seen <= _env_num("LIP_PMUS_STALE_S", 30.0)

    def drain_external(self, max_n: int = 2000) -> int:
        """Feed queued external-venue frames (PM US poller) through on_frame.
        Runs on the frame thread. Timestamps never move the loop clock back."""
        n = 0
        while n < max_n:
            try:
                frame = self.ext_queue.get_nowait()
            except Exception:
                break
            if frame.get("kind") not in ("program", "screen_pmus") and "ts" in frame:
                frame["ts"] = max(float(frame["ts"]), float(self.now or 0.0))
            if frame.get("kind") == "screen_pmus":
                self.pmus_screen = dict(frame.get("stats") or {})
            else:
                rec = getattr(self, "recorder", None)
                if rec is not None:
                    try:
                        rec.record(frame)
                    except Exception:
                        pass
                self.on_frame(frame)
            n += 1
        self.ext_frames_n += n
        self.resting_view = frozenset(self.resting)
        return n

    def _trim_history(self) -> None:
        for name in ("quotes", "cancels", "risk_rows"):
            lst = getattr(self, name)
            if len(lst) > LIST_CAP:
                del lst[: len(lst) - LIST_CAP // 2]

    def _on_trade(self, row: dict, ts: float) -> None:
        trade = dict(row.get("trade") or row.get("msg") or row)
        trade.setdefault("created_time", _iso(ts))
        trade.setdefault("ticker", trade.get("market_ticker") or "")
        if not trade.get("trade_id"):
            return
        for fill in self.sim.apply_trades([trade]):
            self.fills.append(fill)
            self._reduce_resting(fill)
            self._note_fill(fill, ts)
            decision = self.risk.record_fill(1, now=ts)
            if not decision.allowed:
                self.kill = {"reason": decision.reason, "cancel_all": decision.cancel_all,
                             "paper": self.mode == "paper"}
                self._cancel_all(decision.reason)

    # ------------------------------------------------------------ patch 15
    def _note_fill(self, fill: dict, ts: float) -> None:
        """Inventory, markout marks, log line; optional side cooldown + pull."""
        market = str(fill["market_ticker"])
        side = str(fill.get("side"))
        count = float(fill.get("count") or 0)
        price = float(fill.get("price_cents") or 0)
        pos = self.position.setdefault(market, {"yes": 0.0, "no": 0.0, "yes_cost": 0.0, "no_cost": 0.0})
        if side in ("yes", "no"):
            pos[side] += count
            pos[f"{side}_cost"] += count * price / 100.0
        mid = self._side_mid_cents(market, side)
        if self._venue(market) == "pmus" and count > 0:
            # PM US maker rebate 0.0125 x C x p x (1-p), per fill, banker's
            # rounded to the cent (https://docs.polymarket.us/fees).
            from mm.accounting import pm_us_maker_rebate_usd
            self.pm_rebate_usd += float(pm_us_maker_rebate_usd(int(round(price)), count))
        self.fill_marks.append({
            "market": market, "side": side, "price_cents": price, "count": count, "ts": ts,
            "mid0": mid, "venue": self._venue(market), "bucket": (getattr(self, "bucket_of", {}) or {}).get(market),
            "markout_60s": None, "markout_300s": None, "markout_1800s": None,
        })
        if len(self.fill_marks) > 2000:
            self.fill_marks = self.fill_marks[-2000:]
        logging.getLogger("lip.risk").info(
            "paper fill %s %s %.0f@%.0fc mid %s unpaired_yes %.0f",
            market, side, count, price, None if mid is None else round(mid, 1),
            pos["yes"] - pos["no"])
        from mm.unattended import skew as _skew
        if _skew.enabled() and side in ("yes", "no"):
            # Patch 18: no cooldown; re-quote both sides skewed by inventory.
            self._skew_requote(market, ts)
            return
        cool = _env_num("LIP_FILL_COOLDOWN_S", 0.0)
        if cool > 0 and side in ("yes", "no"):
            self.cooldown[(market, side)] = max(self.cooldown.get((market, side), 0.0), ts + cool)
            # Stop buying more of what was just hit. The opposite side stays:
            # if it fills it pairs the inventory into a $1 settlement.
            self._drop_side(market, side, "fill_cooldown")

    # ------------------------------------------------------------ patch 18
    def _inv_frac(self, market: str) -> float:
        """Inventory as a fraction of the unpaired-$ caps (max of market, event)."""
        cap_m = _env_num("LIP_MARKET_INV_CAP_USD", 0.0) or _env_num("LIP_SKEW_REF_USD", 25.0)
        cap_e = _env_num("LIP_EVENT_INV_CAP_USD", 0.0)
        frac = self._unpaired_usd(market) / cap_m if cap_m > 0 else 0.0
        if cap_e > 0:
            ev = self._event_of(market)
            held = sum(self._unpaired_usd(m) for m in self.position if self._event_of(m) == ev)
            frac = max(frac, held / cap_e) if self._unpaired_usd(market) > 0 else frac
        return frac

    def _skew_target(self, market: str, yes_cents: int, no_cents: int) -> tuple:
        from mm.unattended import skew as _skew
        pos = self.position.get(market)
        net = 0.0 if not pos else float(pos["yes"]) - float(pos["no"])
        if not net or market not in self.accruals:
            return int(yes_cents), int(no_cents), None
        yb, nb = self._best(market)
        try:
            yr, nr = self._refs(market)
        except Exception:
            yr, nr = None, None
        info = _skew.skew_prices(int(yes_cents), int(no_cents), net_yes=net,
                                 frac=self._inv_frac(market), best_yes=yb, best_no=nb,
                                 df=self.programs[market].discount_factor,
                                 yes_ref=yr, no_ref=nr)
        return info["yes_cents"], info["no_cents"], info

    def _skew_status(self) -> dict:
        from mm.unattended import skew as _skew
        if not _skew.enabled():
            return {"enabled": False}
        out = dict(self.skew_stats, enabled=True)
        out["skewed_now"] = sum(1 for q in self.resting.values()
                                if (q.get("skew") or {}).get("agg") or (q.get("skew") or {}).get("back"))
        return out

    def _skew_requote(self, market: str, ts: float) -> None:
        quote = self.resting.get(market)
        if quote is None or market not in self.accruals:
            return
        yr, nr = self._refs(market)
        y = int(yr) if yr is not None else int(quote["yes_cents"])
        n = int(nr) if nr is not None else int(quote["no_cents"])
        size = max(float(quote.get("yes") or 0), float(quote.get("no") or 0))
        sides = tuple(sd for sd in ("yes", "no") if not self._side_blocked(market, sd, ts))
        if size <= 0 or not sides:
            self._cancel(market, "skew_no_side")
            return
        best0 = quote.get("best0")
        self.skew_stats["requotes"] = self.skew_stats.get("requotes", 0) + 1
        if self._quote(market, y, n, size, ts, sides=sides) and best0 is not None and market in self.resting:
            self.resting[market]["best0"] = best0

    def _drop_side(self, market: str, side: str, reason: str) -> None:
        quote = self.resting.get(market)
        if quote is None:
            return
        quote[side] = 0.0
        self.sim.untrack(f"{market}:{side}")
        other = "no" if side == "yes" else "yes"
        if float(quote.get(other) or 0) <= 0:
            self._cancel(market, reason)
            return
        self.accruals[market].set_resting(self._orders(market, quote))
        add = Decimal(int(quote[f"{other}_cents"])) / Decimal(100) * Decimal(str(quote[other]))
        self._release(market)
        self.risk.commit(market, self._venue(market), add)
        self.committed[market] = add
        self.cancels.append({"market": market, "reason": f"{reason}:{side}", "ts": self.now})
        self.cancels_total += 1

    def _unpaired(self, market: str, side: str) -> float:
        pos = self.position.get(market)
        if not pos:
            return 0.0
        other = "no" if side == "yes" else "yes"
        return float(pos[side]) - float(pos[other])

    def _unpaired_usd(self, market: str) -> float:
        pos = self.position.get(market)
        if not pos:
            return 0.0
        out = 0.0
        for side in ("yes", "no"):
            extra = self._unpaired(market, side)
            if extra > 0 and pos[side] > 0:
                out += extra * pos[f"{side}_cost"] / pos[side]
        return out

    def _event_of(self, market: str) -> str:
        prog = self.programs.get(market)
        if prog is None:
            return market
        return prog.event_ticker or market.rsplit("-", 1)[0]

    def _side_blocked(self, market: str, side: str, ts: float) -> str:
        if self.cooldown.get((market, side), 0.0) > ts:
            return "fill_cooldown"
        cap_m = _env_num("LIP_MARKET_INV_CAP_USD", 0.0)
        if cap_m > 0 and self._unpaired(market, side) > 0 and self._unpaired_usd(market) >= cap_m:
            return "market_inventory"
        cap_e = _env_num("LIP_EVENT_INV_CAP_USD", 0.0)
        if cap_e > 0 and self._unpaired(market, side) >= 0:
            ev = self._event_of(market)
            held = sum(self._unpaired_usd(m) for m in self.position if self._event_of(m) == ev)
            if held >= cap_e:
                return "event_inventory"
        return ""

    def _policy_block(self, market: str, ts: float) -> str:
        if self.cooldown.get((market, "*"), 0.0) > ts:
            return "move_cooldown"
        if self._in_event_window(market, ts):
            return "event_window"
        return ""

    def _in_event_window(self, market: str, ts: float) -> bool:
        hours = event_window_hours()
        prog = self.programs.get(market)
        if hours <= 0 or prog is None or prog.series.upper() in proven_series():
            return False
        anchor = event_anchor_ts(market, prog.occurrence_ts)
        return anchor is not None and ts >= anchor - hours * 3600.0

    def _refs(self, market: str, size: float | None = None):
        """Quote rungs from the live book under the market's venue rules."""
        from mm.unattended.feed import reference_cents
        prog = self.programs[market]
        book = self.accruals[market].book.book
        if prog.venue == "pmus":
            from mm.selector import pmus_side_rung
            if size is None:
                q = self.resting.get(market) or {}
                size = max(float(q.get("yes") or 0), float(q.get("no") or 0)) or self.chunk
            yb, nb = _bids(book.yes_bids), _bids(book.no_bids)
            return (pmus_side_rung(yb, nb, size, prog.target_size, prog.discount_factor),
                    pmus_side_rung(nb, yb, size, prog.target_size, prog.discount_factor))
        return (reference_cents(_bids(book.yes_bids), prog.target_size),
                reference_cents(_bids(book.no_bids), prog.target_size))

    def _best(self, market: str):
        book = self.accruals[market].book.book
        return (max((lvl.price_cents for lvl in book.yes_bids), default=None),
                max((lvl.price_cents for lvl in book.no_bids), default=None))

    def _fv_drop(self, market: str, ts: float) -> tuple:
        """Patch 16: sides to withhold because external fair value disagrees with Kalshi mid."""
        from mm.unattended.fairvalue import enabled, fv_drop_sides
        if self.fv is None or not enabled() or self._venue(market) != "kalshi":
            return ()
        self._fv_wanted.add(market)
        row = self.fv.get(market, now=time.time())
        if row is None:
            self._fv_state.pop(market, None)
            return ()
        yb, nb = self._best(market)
        drop = fv_drop_sides(row["fv_cents"], yb, nb,
                             float(row.get("thr") or _env_num("LIP_FV_DISAGREE_CENTS", 8.0)),
                             _env_num("LIP_FV_PULL_BOTH", 0.0) > 0)
        if drop and self._fv_state.get(market) != drop:
            logging.getLogger("lip.risk").info(
                "fv guard %s: withhold %s fv=%.1fc yes_bid=%s no_bid=%s pm=%r", market,
                ",".join(drop), row["fv_cents"], yb, nb, str(row.get("pm_question", ""))[:60])
        self._fv_state[market] = drop
        return drop

    def _pull_one(self, market: str, reason: str, ts: float, cool_s: float) -> None:
        if cool_s > 0:
            self.cooldown[(market, "*")] = max(self.cooldown.get((market, "*"), 0.0), ts + cool_s)
        self.pulls[reason] = self.pulls.get(reason, 0) + 1
        logging.getLogger("lip.risk").info("pull %s: %s", market, reason)
        self._cancel(market, reason)

    def _guard_resting(self, ts: float) -> None:
        """Once a second: event window, trade-through, fast move, re-peg."""
        move = _env_num("LIP_PULL_MOVE_CENTS", 0.0)
        cool = _env_num("LIP_MOVE_COOLDOWN_S", 600.0)
        repeg = _env_num("LIP_REPEG_MIN_S", 0.0)
        for market in list(self.resting):
            quote = self.resting.get(market)
            if quote is None or market not in self.accruals:
                continue
            if self._in_event_window(market, ts):
                self._pull_one(market, "event_window", ts, 0.0)
                continue
            if not self.accruals[market].book.book.is_usable():
                continue
            if not self._book_fresh(market, ts):
                self._pull_one(market, "pmus_stale_book", ts, 0.0)
                continue
            yb, nb = self._best(market)
            on = {sd: float(quote.get(sd) or 0) > 0 for sd in ("yes", "no")}
            if _env_num("LIP_CROSS_GUARD", 0.0) > 0:
                crossed = ((on["yes"] and nb is not None and int(quote["yes_cents"]) + nb >= 100)
                           or (on["no"] and yb is not None and int(quote["no_cents"]) + yb >= 100))
                if crossed:
                    if self.mode == "paper":
                        self._paper_cross_fill(market, quote, yb, nb, ts)
                    self._pull_one(market, "trade_through", ts, cool)
                    continue
            fv_drop = self._fv_drop(market, ts)
            if any(on[sd] and sd in fv_drop for sd in ("yes", "no")):
                keep = tuple(sd for sd in ("yes", "no") if on[sd] and sd not in fv_drop)
                if keep:
                    self.pulls["fv_disagree"] = self.pulls.get("fv_disagree", 0) + 1
                    size = max(float(quote.get("yes") or 0), float(quote.get("no") or 0))
                    self._quote(market, int(quote["yes_cents"]), int(quote["no_cents"]), size, ts,
                                sides=keep, skewed=True)
                else:
                    self._pull_one(market, "fv_disagree", ts, 0.0)
                continue
            opp0 = quote.get("best0") or (None, None)
            if move > 0:
                # The opposite bid paying up toward our price is the adverse move.
                fast = ((on["yes"] and nb is not None and opp0[1] is not None and nb - opp0[1] >= move)
                        or (on["no"] and yb is not None and opp0[0] is not None and yb - opp0[0] >= move))
                if fast:
                    self._pull_one(market, "fast_move", ts, cool)
                    continue
            if repeg > 0 and ts - self._repeg_at.get(market, 0.0) >= repeg:
                yr, nr = self._refs(market)
                new_y = int(quote["yes_cents"]) if (yr is None or not on["yes"]) else int(yr)
                new_n = int(quote["no_cents"]) if (nr is None or not on["no"]) else int(nr)
                from mm.unattended import skew as _skew
                if _skew.enabled():
                    sy, sn, _info = self._skew_target(market, new_y, new_n)
                    new_y = sy if on["yes"] else new_y
                    new_n = sn if on["no"] else new_n
                if (new_y, new_n) != (int(quote["yes_cents"]), int(quote["no_cents"])):
                    self._repeg_at[market] = ts
                    size = max(float(quote.get("yes") or 0), float(quote.get("no") or 0))
                    sides = tuple(sd for sd in ("yes", "no") if on[sd])
                    best0 = quote.get("best0")
                    if self._quote(market, new_y, new_n, size, ts, sides=sides, skewed=True):
                        self.repegs_n += 1
                        # keep the fast-move anchor from the original placement
                        if best0 is not None and market in self.resting:
                            self.resting[market]["best0"] = best0
        if self.fill_marks:
            self._update_markouts(ts)

    def _paper_cross_fill(self, market: str, quote: dict, yb, nb, ts: float) -> None:
        """Patch 21 (audit): a book that crosses our resting paper bid means a
        real order would have traded with us. Paper used to only pull here,
        which silently dropped exactly the adverse fills. Fill our crossed
        side(s) at our price for min(our size, crossing depth), then the
        caller pulls as before. Orders still in flight (latency) do not fill."""
        book = self.accruals[market].book.book
        for side, opp_levels in (("yes", book.no_bids), ("no", book.yes_bids)):
            size = float(quote.get(side) or 0)
            if size <= 0:
                continue
            price = int(quote[f"{side}_cents"])
            depth = sum(float(l.size) for l in opp_levels if int(l.price_cents) + price >= 100)
            order = self.sim.orders.get(f"{market}:{side}")
            if depth <= 0 or order is None or ts < float(getattr(order, "activation_ts", 0.0)):
                continue
            count = min(size, depth)
            fill = {"market_ticker": market, "side": side, "price_cents": price, "count": count,
                    "ts": ts, "source": "paper_cross", "trade_id": f"cross:{market}:{side}:{ts:.3f}"}
            self.fills.append(fill)
            self._reduce_resting(fill)
            self._note_fill(fill, ts)
            decision = self.risk.record_fill(1, now=ts)
            if not decision.allowed:
                self.kill = {"reason": decision.reason, "cancel_all": decision.cancel_all,
                             "paper": self.mode == "paper"}
                self._cancel_all(decision.reason)
                return
            quote = self.resting.get(market) or quote

    def _update_markouts(self, ts: float) -> None:
        for mark in self.fill_marks[-500:]:
            for horizon in (60, 300, 1800):
                key = f"markout_{horizon}s"
                if mark[key] is None and ts >= mark["ts"] + horizon:
                    mid = self._side_mid_cents(mark["market"], str(mark["side"]))
                    if mid is not None:
                        mark[key] = round(float(mark["count"]) * (mid - float(mark["price_cents"])) / 100.0, 4)

    def markout_summary(self) -> dict:
        out = {"fills": len(self.fill_marks)}
        for horizon in (60, 300, 1800):
            vals = [m[f"markout_{horizon}s"] for m in self.fill_marks if m[f"markout_{horizon}s"] is not None]
            out[f"markout_{horizon}s_usd"] = round(sum(vals), 4)
            out[f"markout_{horizon}s_n"] = len(vals)
        out["unpaired_usd"] = round(sum(self._unpaired_usd(m) for m in self.position), 4)
        return out

    def _size_curve(self, km, ladder, sides_on: tuple, penalty_100: float) -> list:
        """[(size, value $/day, capital $, yes_c, no_c)] along the ladder, at the
        reference rungs. value = plan net/day - markout penalty (scaled by size)."""
        from mm.selector import quote_economics, kalshi_one_sided_share, reward_per_day
        from mm.session_gates import max_contracts_for_fill
        lim = self.risk.limits
        per_market = float(lim.per_market_usd) * alloc_cap_fraction()
        out = []
        for size in ladder:
            net, capital, share2, yc, nc = quote_economics(km, float(size))
            if yc <= 0 or nc <= 0:
                break
            if len(sides_on) == 1:
                side = sides_on[0]
                price = yc if side == "yes" else nc
                cost2 = reward_per_day(share2, km) - net
                share1 = kalshi_one_sided_share(km, side, price, float(size))
                net = reward_per_day(share1, km) - cost2 / 2.0
                capital = price / 100.0 * float(size)
            if size > max_contracts_for_fill(yc, self.fill_cap) or size > max_contracts_for_fill(nc, self.fill_cap):
                break
            if capital > per_market + 1e-9:
                break
            value = net - penalty_100 * float(size) / 100.0 * (len(sides_on) / 2.0)
            out.append((float(size), value, capital, int(yc), int(nc)))
        return out

    def _reduce_resting(self, fill: dict) -> None:
        market = fill["market_ticker"]
        quote = self.resting.get(market)
        accrual = self.accruals.get(market)
        if quote is None or accrual is None:
            return
        side = fill["side"]
        left = max(0.0, float(quote[side]) - float(fill["count"]))
        quote[side] = left
        accrual.set_resting(self._orders(market, quote))

    def _orders(self, market: str, quote: dict) -> list[RestingOrder]:
        orders = []
        for side in ("yes", "no"):
            size = float(quote[side])
            if size > 0:
                orders.append(RestingOrder(side, int(quote[f"{side}_cents"]), size, in_book=0))
        return orders

    def _books_ready(self) -> bool:
        if not self.programs:
            return False
        have = 0
        for market in self.programs:
            book = self.accruals[market].book.book
            if book.yes_bids or book.no_bids:
                have += 1
        return have >= self.books_ready_fraction * len(self.programs)

    def _maybe_select(self, ts: float) -> None:
        if not self.programs:
            return
        if self.last_select_ts is None and self.first_select_warmup_s > 0:
            if self._first_frame_ts is None:
                self._first_frame_ts = ts
            if ts - self._first_frame_ts < self.first_select_warmup_s and not self._books_ready():
                return
        if self.last_select_ts is not None and ts - self.last_select_ts < self.select_every:
            return
        self._select(ts)

    def _markets(self) -> list[KalshiMarket]:
        rows = []
        for market, prog in self.programs.items():
            book = self.accruals[market].book.book
            rows.append(KalshiMarket(
                market=market,
                series=prog.series,
                period_reward_usd=prog.period_reward_usd,
                period_seconds=prog.period_seconds,
                seconds_left=max(0.0, prog.end_ts - self.now),
                discount_factor=prog.discount_factor,
                target_size=prog.target_size,
                yes_bids=_bids(book.yes_bids),
                no_bids=_bids(book.no_bids),
                days_to_settle=(max(0.0, (prog.close_ts - self.now) / 86400.0)
                                if prog.days_from_close and prog.close_ts is not None and self.now
                                else prog.days_to_settle),
                exchange_index=prog.exchange_index,
                shard_cash_usd=prog.shard_cash_usd,
                category=prog.category,
                venue=prog.venue,
                max_spread_usd=prog.max_spread_usd,
                sports_single=prog.sports_single,
                fee_type=prog.fee_type,
                fee_multiplier=prog.fee_multiplier,
            ))
        return rows

    def _select(self, ts: float) -> None:
        self._select_t0 = time.time()
        self.selection_count += 1
        self.last_select_ts = ts
        markets = self._markets()
        live = self.mode != "paper"
        lim = self.risk.limits
        frac = alloc_cap_fraction()
        per_market = float(lim.per_market_usd) * frac
        per_series = float(lim.per_series_usd) * frac
        venue_budget = self.venue_budgets()
        budget = sum(venue_budget.values())
        self.alloc_budget_usd = budget
        self.venue_budget = venue_budget
        # allocate/optimize_sizes are economics + eligibility passes here. Their
        # own cash pool must not bind, or their raw (unpenalized) $/day greedy
        # truncates the list before the markout-penalized rank pass below,
        # which is the only allocator of the real budget.
        pool = float(self.bankroll) * 100.0
        if not live and _env_num("LIP_FAST_ALLOCATE", 0.0) > 0:
            from mm.selector import fast_allocate
            selection = fast_allocate(markets, per_market_usd=per_market, chunk=self.chunk,
                                      single_fill_cap_usd=self.fill_cap)
        else:
            selection = allocate(
                markets, bankroll=pool, chunk=self.chunk, max_size=self.chunk,
                per_market_usd=per_market, per_series_usd=per_series,
                per_category_usd=pool, live=live, series_stats=self.series_stats,
                single_fill_cap_usd=self.fill_cap,
            )
        self.excluded = list(selection.excluded)
        sized = optimize_sizes(
            markets, bankroll=pool, per_market_usd=per_market,
            per_event_usd=per_series, total_usd=pool,
            sizes=(self.chunk,), markout_usd_per_contract=0.0,
            single_fill_cap_usd=self.fill_cap,
        )
        chosen = {row.market: row for row in sized.chosen}
        taken = {row.market for row in selection.taken}
        plan = {}
        for row in selection.taken:
            plan[row.market] = {"net_per_day": float(row.net_per_day),
                                "capital_usd": float(row.capital_usd)}
        for market, row in chosen.items():
            plan.setdefault(market, {})
            plan[market].update({"objective": float(row.objective), "share": float(row.share),
                                 "sized_capital_usd": float(row.capital_usd)})
        self.last_plan = plan
        # Every quote is re-placed or cancelled below: rebuild commitments
        # from zero in rank order so a cap only ever skips the weakest.
        for market in list(self.committed):
            self._release(market)
        wanted = []
        for market in self.programs:
            row = chosen.get(market)
            if row is None or market not in taken or row.size <= 0:
                self._cancel(market, "not_selected")
                continue
            info = plan.get(market, {})
            cap = info.get("capital_usd") or info.get("sized_capital_usd") or 0.0
            per_dollar = rank_live_score(info.get("net_per_day") or 0.0,
                                         self.programs[market].rank_penalty_per_day, cap)
            info["rank"] = per_dollar
            wanted.append((per_dollar, market, row))
        wanted.sort(key=lambda item: (-item[0], item[1]))
        self.cap_skips = []
        self.rank_skips = []
        min_rank = rank_min_score()
        kept = []
        for item in wanted:
            if item[0] <= min_rank:
                self._cancel(item[1], "rank_nonpositive")
                self.rank_skips.append(item[1])
            else:
                kept.append(item)
        if self.rank_skips:
            logging.getLogger("lip.risk").info(
                "selection %d: %d skipped for rank <= %.4f after markout penalty",
                self.selection_count, len(self.rank_skips), min_rank)
        wanted = kept
        if not hasattr(self, "bucket_of"):
            self.bucket_of = {}
        dmin = durable_min_days()

        def _bucket(market: str) -> str:
            prog = self.programs[market]
            if prog.close_ts is not None and self.now:
                days = (prog.close_ts - ts) / 86400.0
            else:
                days = prog.days_to_settle or 0.0
            return "durable" if days >= dmin else "short"

        # Patch 15: policy filters (cooldown after a fast move, event window)
        # and per-side inventory/fill blocks before any capital is assigned.
        self.policy_skips = []
        sides_of: dict[str, tuple] = {}
        kept = []
        for item in wanted:
            market = item[1]
            why = self._policy_block(market, ts)
            if not why:
                blocked = {sd: self._side_blocked(market, sd, ts) for sd in ("yes", "no")}
                sides = tuple(sd for sd in ("yes", "no") if not blocked[sd])
                if not sides:
                    why = blocked["yes"] or blocked["no"]
                else:
                    sides_of[market] = sides
            if why:
                self._cancel(market, why)
                self.policy_skips.append((market, why))
                continue
            kept.append(item)
        wanted = kept
        bucket_now = {m: _bucket(m) for _p, m, _r in wanted}
        bucket_budget = split_bucket_budgets(
            budget, durable_reserve(),
            any(b == "durable" for b in bucket_now.values()),
            any(b == "short" for b in bucket_now.values()))
        self.bucket_budget = bucket_budget
        from mm.fair_value import family_for_series
        by_name = {m.market: m for m in markets}
        ladder = size_ladder(self.chunk)
        curves: dict[str, list] = {}
        for _per, market, row in wanted:
            km = by_name.get(market)
            if km is None:
                continue
            if len(ladder) == 1 and sides_of.get(market) == ("yes", "no"):
                # Legacy single size: the optimizer row exactly as before.
                size = float(row.size)
                add = (int(row.yes_cents) + int(row.no_cents)) / 100.0 * size
                val = float(plan.get(market, {}).get("rank") or 0.0) * add
                curves[market] = [(size, val, add, int(row.yes_cents), int(row.no_cents))]
                continue
            curve = self._size_curve(km, ladder, sides_of[market],
                                     self.programs[market].rank_penalty_per_day)
            if curve:
                curves[market] = curve
        min_rank = rank_min_score()
        spent_bucket = {"durable": 0.0, "short": 0.0}
        spent = {"series": {}, "under": {}, "event": {}, "venue": {}, "total": 0.0}
        event_frac = _env_num("LIP_EVENT_CAP_FRAC", 0.0)
        state = {m: -1 for m in curves}
        why_stop: dict[str, str] = {}

        def _step(market):
            """Best next ladder jump from the current size (upper concave hull):
            the jump with the highest marginal $/day per marginal $ capital."""
            curve = curves[market]
            cur = state[market]
            v0, c0 = (0.0, 0.0) if cur < 0 else (curve[cur][1], curve[cur][2])
            best = None
            for j in range(cur + 1, len(curve)):
                dv, dc = curve[j][1] - v0, curve[j][2] - c0
                if dc <= 1e-12:
                    continue
                if cur < 0 and (curve[j][1] <= 0 or curve[j][1] / curve[j][2] <= min_rank):
                    continue
                ratio = dv / dc
                if ratio <= 0:
                    continue
                if best is None or ratio > best[0] + 1e-12:
                    best = (ratio, j, dc)
            return best

        def _fits(market, dc, use_buckets):
            series = self.programs[market].series
            family = family_for_series(series)
            bkt = bucket_now[market]
            ev = self._event_of(market)
            if spent["total"] + dc > budget + 1e-9:
                return f"alloc_budget {spent['total'] + dc:.0f} > {budget:.0f}"
            vn = self._venue(market)
            if spent["venue"].get(vn, 0.0) + dc > venue_budget.get(vn, 0.0) + 1e-9:
                return f"alloc_venue_{vn} {spent['venue'].get(vn, 0.0) + dc:.0f} > {venue_budget.get(vn, 0.0):.0f}"
            if use_buckets and spent_bucket[bkt] + dc > bucket_budget[bkt] + 1e-9:
                return f"alloc_bucket_{bkt} {spent_bucket[bkt] + dc:.0f} > {bucket_budget[bkt]:.0f}"
            if spent["series"].get(series, 0.0) + dc > per_series:
                return f"alloc_series {series}"
            if (family in ("commodity", "crypto", "weather")
                    and spent["under"].get(family, 0.0) + dc > float(lim.per_underlying_usd) * frac):
                return f"alloc_underlying {family}"
            if event_frac > 0 and spent["event"].get(ev, 0.0) + dc > event_frac * budget + 1e-9:
                return f"alloc_event {ev}"
            return ""

        for use_buckets in (True, False):
            # Pass 2: a bucket may use budget the other bucket could not place.
            active = set(curves)
            while active:
                best = None
                for market in list(active):
                    st = _step(market)
                    if st is None:
                        active.discard(market)
                        continue
                    key = (st[0], market)
                    if best is None or key > best[0]:
                        best = (key, market, st)
                if best is None:
                    break
                _key, market, (ratio, j, dc) = best
                why = _fits(market, dc, use_buckets)
                if why:
                    why_stop[market] = why
                    active.discard(market)
                    continue
                why_stop.pop(market, None)
                state[market] = j
                series = self.programs[market].series
                spent["total"] += dc
                spent["venue"][self._venue(market)] = spent["venue"].get(self._venue(market), 0.0) + dc
                spent_bucket[bucket_now[market]] += dc
                spent["series"][series] = spent["series"].get(series, 0.0) + dc
                fam = family_for_series(series)
                spent["under"][fam] = spent["under"].get(fam, 0.0) + dc
                ev = self._event_of(market)
                spent["event"][ev] = spent["event"].get(ev, 0.0) + dc
            if spent["total"] >= budget - 1e-6:
                break
        order = sorted(curves, key=lambda m: (-(curves[m][0][1] / curves[m][0][2]
                                                if curves[m][0][2] else 0.0), m))
        for market in order:
            j = state[market]
            if j < 0:
                self._cap_skip(market, why_stop.get(market, "alloc_no_positive_step"))
                continue
            size, val, cap_usd, yc, nc = curves[market][j]
            info = plan.setdefault(market, {})
            info.update({"size": size, "value_per_day": val, "chosen_capital_usd": cap_usd,
                         "sides": list(sides_of.get(market, ("yes", "no")))})
            if self._quote(market, yc, nc, size, ts, sides=sides_of.get(market, ("yes", "no"))):
                self.bucket_of[market] = bucket_now[market]
        for _per, market, _row in wanted:
            if market not in curves:
                self._cancel(market, "no_curve")
        self.select_ms = round((time.time() - getattr(self, "_select_t0", time.time())) * 1000.0, 1)
        try:
            self._dump_selection(chosen, taken, plan, budget)
        except Exception:
            logging.getLogger("lip.risk").exception("selection dump failed")
        if self.cap_skips:
            logging.getLogger("lip.risk").info(
                "selection %d: %d quoted, %d skipped at caps (budget $%.0f)",
                self.selection_count, len(self.resting), len(self.cap_skips), budget)

    def venue_budgets(self) -> dict:
        """Patch 21: unified cross-venue allocation budgets.

        kalshi = alloc fraction x min(per-venue cap, gross cap) (unchanged).
        pmus   = min(LIP_PMUS_BUDGET_USD (300), alloc fraction x gross - kalshi)
                 when any PM US program is loaded, else 0.
        Sum <= alloc fraction x gross cap, so both venues together can never
        breach the shared gross cap; the RiskEngine also checks per-venue and
        gross caps on every quote."""
        lim = self.risk.limits
        frac = alloc_cap_fraction()
        kalshi = min(float(lim.per_venue_usd), float(lim.gross_usd)) * frac
        out = {"kalshi": kalshi, "pmus": 0.0}
        if any(p.venue == "pmus" for p in self.programs.values()):
            room = max(0.0, float(lim.gross_usd) * frac - kalshi)
            out["pmus"] = max(0.0, min(_env_num("LIP_PMUS_BUDGET_USD", 300.0), room,
                                       float(lim.per_venue_usd) * frac))
        return out

    def _dump_selection(self, chosen, taken, plan, budget) -> None:
        path = os.environ.get("LIP_SELECTION_DUMP", "/var/lib/lip-maker/last_selection.json")
        if not path or path == "off" or not os.path.isdir(os.path.dirname(path) or "."):
            return
        import json as _json
        excl = {m: str(w) for m, w in self.excluded}
        rows = []
        for market, prog in self.programs.items():
            info = plan.get(market, {})
            rows.append({
                "market": market, "screen_rank": prog.rank_score,
                "penalty_per_day": prog.rank_penalty_per_day,
                "in_taken": market in taken, "in_chosen": market in chosen,
                "excluded": excl.get(market), "net_per_day": info.get("net_per_day"),
                "capital_usd": info.get("capital_usd") or info.get("sized_capital_usd"),
                "rank": info.get("rank"), "resting": market in self.resting,
                "book_usable": bool(market in self.accruals and self.accruals[market].book.book.is_usable()),
                "bucket": (getattr(self, "bucket_of", {}) or {}).get(market) if market in self.resting else None,
            })
        rows.sort(key=lambda r: -(r["screen_rank"] or -1e9))
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            _json.dump({"ts": self.now, "selection": self.selection_count, "budget": budget,
                        "bucket_budget": getattr(self, "bucket_budget", None),
                        "durable_reserve": durable_reserve(), "durable_min_days": durable_min_days(),
                        "rank_skips": list(getattr(self, "rank_skips", [])),
                        "cap_skips": self.cap_skips, "rows": rows}, fh)
        os.replace(tmp, path)

    def _cap_skip(self, market: str, reason: str) -> None:
        self._cancel(market, f"cap_skip:{reason}")
        self.cap_skips.append({"market": market, "reason": reason, "ts": self.now})
        logging.getLogger("lip.risk").info("cap skip %s: %s", market, reason)

    def _inside_close(self, market: str, ts: float) -> bool:
        close_ts = self.programs[market].close_ts
        if close_ts is None:
            return False
        return inside_close_window(close_ts, ts, pull_before_s=self.pull_before_s)

    def _quote(self, market: str, yes_cents: int, no_cents: int, size: float, ts: float,
               sides: tuple = ("yes", "no"), skewed: bool = False) -> bool:
        skew_info = None
        if not skewed:
            from mm.unattended import skew as _skew
            if _skew.enabled():
                yes_cents, no_cents, skew_info = self._skew_target(market, yes_cents, no_cents)
                if skew_info and (skew_info["agg"] or skew_info["back"]):
                    self.skew_stats["skewed_quotes"] = self.skew_stats.get("skewed_quotes", 0) + 1
                    self.skew_stats["last"] = {"market": market, **skew_info}
        if self.kill is not None:
            self._cancel(market, self.kill["reason"])
            return False
        if self._inside_close(market, ts):
            self._cancel(market, "close_cutoff")
            return False
        size = float(min(
            clamp_contracts(yes_cents, size, self.fill_cap),
            clamp_contracts(no_cents, size, self.fill_cap),
        ))
        if size <= 0:
            self._cancel(market, "fill_cap")
            return False
        sides = tuple(sd for sd in ("yes", "no") if sd in sides)
        if _env_num("LIP_CROSS_GUARD", 0.0) > 0:
            # A bid at or through the opposite implied ask would take, not make.
            yb0, nb0 = self._best(market)
            sides = tuple(sd for sd in sides
                          if not ((sd == "yes" and nb0 is not None and int(yes_cents) + nb0 >= 100)
                                  or (sd == "no" and yb0 is not None and int(no_cents) + yb0 >= 100)))
        fv_drop = self._fv_drop(market, ts) if sides else ()
        if fv_drop and any(sd in fv_drop for sd in sides):
            for sd in sides:
                if sd in fv_drop:
                    self.fv_blocks[sd] = self.fv_blocks.get(sd, 0) + 1
            sides = tuple(sd for sd in sides if sd not in fv_drop)
            if not sides:
                self._cancel(market, "fv_disagree")
                return False
        if not sides:
            self._cancel(market, "would_cross")
            return False
        self._release(market)
        add = (Decimal(yes_cents if "yes" in sides else 0) + Decimal(no_cents if "no" in sides else 0)) \
            / Decimal(100) * Decimal(str(size))
        decision = self.risk.check_quote(market=market, venue=self._venue(market), add_usd=add, now=ts)
        self.risk_rows.append({
            "market": market, "allowed": decision.allowed,
            "reason": decision.reason, "cancel_all": decision.cancel_all,
        })
        if not decision.allowed:
            if str(decision.reason).startswith(CAP_REASONS) and not decision.cancel_all:
                # A position/exposure cap: skip this quote, keep running.
                self._cap_skip(market, str(decision.reason))
                return False
            self.kill = {"reason": decision.reason, "cancel_all": decision.cancel_all,
                         "paper": self.mode == "paper"}
            self._cancel(market, decision.reason)
            if decision.cancel_all:
                self._cancel_all(decision.reason)
            return False
        book = self.accruals[market].book.book
        if self.mode != "paper" and self._venue(market) != "kalshi":
            # Patch 21: PM US is PAPER ONLY. No order path exists for it here.
            self._cancel(market, "pmus_paper_only")
            return False
        if self.mode == "demo":
            if self.poster is None:
                raise UnattendedRefused("demo mode would send without a sender")
            no_bid = book.no_bids[0].price_cents if book.no_bids else 0
            yes_bid = book.yes_bids[0].price_cents if book.yes_bids else 0
            yes_cents = _passive_cents(yes_cents, no_bid)
            no_cents = _passive_cents(no_cents, yes_bid)
            if yes_cents <= 0 or no_cents <= 0:
                self._cancel(market, "would_cross")
                return False
            if "yes" in sides:
                self.poster.place(market=market, side="yes", price_cents=yes_cents,
                                  size=size, opposing_bid_cents=no_bid)
            if "no" in sides:
                self.poster.place(market=market, side="no", price_cents=no_cents,
                                  size=size, opposing_bid_cents=yes_bid)
        else:
            for side, price in (("yes", yes_cents), ("no", no_cents)):
                self.sim.untrack(f"{market}:{side}")
                if side not in sides:
                    continue
                self.sim.track(
                    order_id=f"{market}:{side}", market_ticker=market, side=side,
                    price_cents=price, size=size, book=book, now=ts,
                    program_id=market,
                )
        quote = {"yes": size if "yes" in sides else 0.0, "no": size if "no" in sides else 0.0,
                 "yes_cents": yes_cents, "no_cents": no_cents, "ts": ts}
        if skew_info is not None:
            quote["skew"] = skew_info
        elif skewed and market in self.resting and self.resting[market].get("skew"):
            quote["skew"] = self.resting[market]["skew"]
        try:
            quote["best0"] = self._best(market)
        except Exception:
            quote["best0"] = (None, None)
        self.resting[market] = quote
        self.accruals[market].set_resting(self._orders(market, quote))
        self.risk.commit(market, self._venue(market), add)
        self.committed[market] = add
        self.quotes.append({
            "market": market, "size": size, "yes_cents": yes_cents, "no_cents": no_cents,
            "paper": self.mode == "paper", "ts": ts, "sides": list(sides),
        })
        self.quotes_total += 1
        self.quoted_ever.add(market)
        if self.carry_forward:
            self._trim_history()
        return True

    def _pull(self, ts: float) -> None:
        # Close windows are second-granular; scanning ~1000 programs on every
        # book message saturated the event loop. Once per wall second.
        sec = int(ts)
        if getattr(self, "_pulled_sec", None) == sec:
            return
        self._pulled_sec = sec
        for market in list(self.programs):
            if self._inside_close(market, ts):
                self._cancel(market, "close_cutoff")
        self._guard_resting(ts)

    def _cancel(self, market: str, reason: str) -> None:
        had = market in self.resting
        for side in ("yes", "no"):
            self.sim.untrack(f"{market}:{side}")
        self.resting.pop(market, None)
        accrual = self.accruals.get(market)
        if accrual is not None:
            accrual.set_resting([])
        self._release(market)
        if had:
            self.cancels.append({"market": market, "reason": reason, "ts": self.now})
            self.cancels_total += 1

    def external_kill(self, reason: str) -> None:
        """Patch 17: latch an external (watchdog) kill; cancel all once. Idempotent."""
        if self.kill is not None and str(self.kill.get("reason", "")).startswith("external_kill:"):
            return
        prev = self.kill
        self.kill = {"reason": f"external_kill:{reason}", "cancel_all": True,
                     "paper": self.mode == "paper", "prev": prev}
        self._cancel_all(self.kill["reason"])

    def _cancel_all(self, reason: str) -> None:
        for market in list(self.resting):
            self._cancel(market, reason)

    def _release(self, market: str) -> None:
        prev = self.committed.pop(market, Decimal(0))
        if prev == 0:
            return
        venue = self._venue(market)
        self.risk.market_usd[market] = Decimal(str(self.risk.market_usd.get(market, 0))) - prev
        self.risk.venue_usd[venue] = Decimal(str(self.risk.venue_usd.get(venue, 0))) - prev

    def live_snapshot(self, *, estimates: dict | None = None,
                      session_start_ts: float | None = None, top_n: int | None = None,
                      accrual: dict | None = None) -> dict:
        """Read-only view of the running loop for the status page.

        Does not score open seconds, close elapsed seconds, cancel, or
        touch the simulator. ``estimates`` (market -> Decimal payable USD)
        is optional and computed by the caller via ``live_estimates``.
        """
        if top_n is None:
            try:
                top_n = int(os.environ.get("LIP_STATUS_TOP", 15))
            except (TypeError, ValueError):
                top_n = 15
        est = dict(estimates or {})
        elapsed = None
        if session_start_ts is not None and self.now:
            elapsed = max(0.0, float(self.now) - float(session_start_ts))
        premium = sum(
            (Decimal(str(fill["count"])) * Decimal(int(fill["price_cents"])) / Decimal(100)
             for fill in self.fills),
            Decimal(0),
        )
        selected = []
        for market, quote in self.resting.items():
            prog = self.programs.get(market)
            usd = est.get(market)
            per_day = None
            if usd is not None and elapsed and elapsed >= 60:
                per_day = format((usd / Decimal(str(elapsed)) * Decimal(86400)).quantize(Decimal("0.0001")), "f")
            planned = (getattr(self, "last_plan", {}) or {}).get(market, {})
            _net = planned.get("net_per_day")
            _cap = planned.get("capital_usd", planned.get("sized_capital_usd"))
            _per100 = (float(_net) / float(_cap) * 100.0) if (_net is not None and _cap) else None
            _close = None if prog is None else prog.close_ts
            selected.append({
                "days_to_close": (None if _close is None
                                  else round(max(0.0, (_close - (self.now or time.time())) / 86400.0), 2)),
                "plan_usd_per_day_per_100": None if _per100 is None else round(_per100, 2),
                "rank_score": (round(planned["rank"], 6) if planned.get("rank") is not None
                               else (None if prog is None else prog.rank_score)),
                "screen_rank_score": None if prog is None else prog.rank_score,
                "bucket": (getattr(self, "bucket_of", {}) or {}).get(market),
                "committed_usd": float(self.committed.get(market, 0)),
                "suspect": bool(_per100 is not None and _per100 > suspect_per_100()),
                "plan_net_usd_per_day": planned.get("net_per_day"),
                "plan_capital_usd": planned.get("capital_usd", planned.get("sized_capital_usd")),
                "market": market,
                "series": None if prog is None else prog.series,
                "size": quote.get("yes"),
                "yes_cents": quote.get("yes_cents"),
                "no_cents": quote.get("no_cents"),
                "period_reward_usd": None if prog is None else prog.period_reward_usd,
                "est_usd": None if usd is None else format(usd, "f"),
                "est_usd_per_day": per_day,
                "est_raw_usd": (None if not accrual or market not in accrual
                                else format(accrual[market]["raw_usd"].quantize(Decimal("0.000001")), "f")),
                "est_raw_usd_per_day": (None if not accrual or market not in accrual or not elapsed or elapsed < 60
                                        else round(float(accrual[market]["raw_usd"]) / elapsed * 86400.0, 4)),
                "seconds_known_unknown_forfeited": (None if not accrual or market not in accrual
                                                    else [accrual[market]["known"], accrual[market]["unknown"],
                                                          accrual[market]["forfeited"]]),
                "est_share_now": (None if not accrual or market not in accrual
                                  or accrual[market].get("share") is None
                                  else round(float(accrual[market]["share"]), 4)),
                "sides": [sd for sd in ("yes", "no") if float(quote.get(sd) or 0) > 0],
            })

        def _key(row):
            v = row["rank_score"]
            return (-(float(v) if v is not None else -1e9), -(row["period_reward_usd"] or 0.0))

        reasons: dict[str, int] = {}
        for _market, why in self.excluded:
            base = re.sub(r"_[0-9.]+d$", "", str(why))
            base = base.split(":", 1)[0] if base.startswith("series_gate") else base
            reasons[base] = reasons.get(base, 0) + 1
        shard_unknown_n = sum(1 for prog in self.programs.values() if prog.exchange_index is None)

        selected.sort(key=_key)
        total = sum(est.values(), Decimal(0))
        day = datetime.fromtimestamp(self.now or time.time(), timezone.utc).date().isoformat()
        return {
            "paper": self.mode == "paper",
            "demo": self.mode == "demo",
            "mode": self.mode,
            "live_armed": False,
            "socket_opened": self.socket_opened,
            "stage": "running" if self.selection_count else ("warmup" if self.programs else "collecting"),
            "markets": [row["market"] for row in selected],
            "programs_loaded": int((self.screen_stats or {}).get("programs_total") or len(self.programs)),
            "programs_fed": len(self.programs),
            "screen": self.screen_stats or None,
            "suspect_n": sum(1 for row in selected if row["suspect"]),
            "paper_capital_usd": round(float(sum(self.committed.values(), Decimal(0))), 2),
            "alloc_budget_usd": None if self.alloc_budget_usd is None else round(self.alloc_budget_usd, 2),
            "bankroll_usd": self.bankroll,
            "risk_limits": {
                "per_market_usd": float(self.risk.limits.per_market_usd),
                "per_series_usd": float(self.risk.limits.per_series_usd),
                "per_venue_usd": float(self.risk.limits.per_venue_usd),
                "gross_usd": float(self.risk.limits.gross_usd),
                "daily_loss_usd": float(self.risk.limits.daily_loss_usd),
            },
            "rank_skips_n": len(getattr(self, "rank_skips", []) or []),
            "cap_skips_n": len(self.cap_skips),
            "cap_skips": self.cap_skips[:10],
            "selection_count": self.selection_count,
            "selected_n": len(self.resting),
            "selected_top": selected[:top_n],
            "resting_n": len(self.resting),
            "quotes_n": self.quotes_total or len(self.quotes),
            "cancels_n": self.cancels_total or len(self.cancels),
            "venues": self.venue_report(est, accrual),
            "fills_n": len(self.fills),
            "excluded_n": len(self.excluded),
            "excluded_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "programs_shard_unknown": shard_unknown_n,
            "estimated_usd": format(total, "f"),
            "estimated_usd_note": "payable after the $1 per-period minimum; see estimated_raw_usd",
            "buckets": self.bucket_report(accrual),
            "durable_reserve": durable_reserve(),
            "estimated_raw_usd": (None if accrual is None else format(
                sum((v["raw_usd"] for v in accrual.values()), Decimal(0)).quantize(Decimal("0.000001")), "f")),
            "accrual_seconds": (None if accrual is None else {
                k: sum(int(v.get(k, 0)) for v in accrual.values())
                for k in ("known", "unknown", "forfeited", "idle")}),
            "fills_detail": list(self.fill_marks)[-20:],
            "markouts": self.markout_summary(),
            "policy_skips": dict(__import__("collections").Counter(w for _m, w in self.policy_skips)),
            "pulls": dict(self.pulls),
            "repegs_n": self.repegs_n,
            "skew": self._skew_status(),
            "recorder": (self.recorder.summary() if getattr(self, "recorder", None) is not None
                         else {"enabled": False}),
            "pmus": (self.pmus.summary() if getattr(self, "pmus", None) is not None
                     else {"enabled": False}),
            "fair_value": (dict(self.fv.summary(), blocks=dict(self.fv_blocks),
                                withheld=sorted(k for k, v in self._fv_state.items() if v))
                           if self.fv is not None else {"enabled": False}),
            "select_ms": getattr(self, "select_ms", None),
            "size_ladder": size_ladder(self.chunk),
            "estimates_partial": estimates is None,
            "session_elapsed_s": None if elapsed is None else round(elapsed, 1),
            "last_frame_ts": self.now or None,
            "kill": self.kill,
            "pnl_usd": format(-premium, "f"),
            "rewards_usd": "0",
            "day": day,
        }

    def venue_report(self, est: dict | None = None, accrual: dict | None = None) -> dict:
        """Patch 21: per-venue programs, resting, capital, est rewards, fills. Read-only."""
        budgets = getattr(self, "venue_budget", None) or {}
        out = {}
        for vn in VENUES:
            mk = [m for m in self.resting if self._venue(m) == vn]
            out[vn] = {
                "programs_fed": sum(1 for p in self.programs.values() if p.venue == vn),
                "resting_n": len(mk),
                "capital_usd": round(float(sum((self.committed.get(m, Decimal(0)) for m in mk), Decimal(0))), 2),
                "budget_usd": round(float(budgets.get(vn, 0.0)), 2),
                "risk_venue_usd": round(float(self.risk.venue_usd.get(vn, 0) or 0), 2),
                "est_usd": format(sum(((est or {}).get(m, Decimal(0)) for m in mk), Decimal(0)), "f"),
                "est_raw_usd": (None if accrual is None else round(sum(
                    float(v["raw_usd"]) for m, v in accrual.items() if self._venue(m) == vn), 6)),
                "fills_n": sum(1 for f in self.fills if self._venue(str(f.get("market_ticker"))) == vn),
                "plan_net_usd_per_day": round(sum(float((self.last_plan.get(m) or {}).get("value_per_day") or 0.0)
                                                  for m in mk), 4),
            }
        out["pmus"]["rebates_usd"] = round(self.pm_rebate_usd, 4)
        out["pmus"]["screen"] = getattr(self, "pmus_screen", None)
        out["ext_frames_n"] = self.ext_frames_n
        out["refeeds_n"] = self.refeeds_n
        return out

    def _side_mid_cents(self, market: str, side: str):
        accrual = self.accruals.get(market)
        if accrual is None:
            return None
        book = accrual.book.book
        yb = max((lvl.price_cents for lvl in book.yes_bids), default=None)
        nb = max((lvl.price_cents for lvl in book.no_bids), default=None)
        if yb is None or nb is None:
            return None
        yes_mid = (yb + (100 - nb)) / 2.0
        return yes_mid if side == "yes" else 100.0 - yes_mid

    def bucket_report(self, accrual: dict | None = None) -> dict:
        """Per-bucket capital, raw est rewards, fills, premium, markout (MTM vs
        current mid) and pnl = markout + raw rewards. Read-only."""
        tags = getattr(self, "bucket_of", {}) or {}
        out = {b: {"budget_usd": round(float((getattr(self, "bucket_budget", None) or {}).get(b, 0.0)), 2),
                   "selected_n": 0, "selected": [], "capital_usd": 0.0, "raw_est_usd": 0.0,
                   "fills_n": 0, "premium_usd": 0.0, "markout_usd": 0.0, "pnl_usd": 0.0}
               for b in ("durable", "short")}
        for market in self.resting:
            b = tags.get(market, "short")
            out[b]["selected_n"] += 1
            out[b]["selected"].append(market)
            out[b]["capital_usd"] += float(self.committed.get(market, 0) or 0)
        for market, info in (accrual or {}).items():
            b = tags.get(market)
            if b in out:
                out[b]["raw_est_usd"] += float(info["raw_usd"])
        for fill in self.fills:
            market = fill.get("market_ticker")
            b = tags.get(market, "short")
            count = float(fill.get("count") or 0)
            price = float(fill.get("price_cents") or 0)
            out[b]["fills_n"] += 1
            out[b]["premium_usd"] += count * price / 100.0
            mid = self._side_mid_cents(market, str(fill.get("side")))
            if mid is not None:
                out[b]["markout_usd"] += count * (mid - price) / 100.0
        for b in out.values():
            b["pnl_usd"] = b["markout_usd"] + b["raw_est_usd"]
            for k in ("capital_usd", "raw_est_usd", "premium_usd", "markout_usd", "pnl_usd"):
                b[k] = round(b[k], 6)
        return out

    def live_accrual(self, markets=None) -> dict:
        """Raw (pre-$1-floor) accrual and second counts per market. Read-only.

        ``estimated_usd`` is the payable figure: a period total under $1 pays
        $0, so it stays 0 for the first hours even while seconds score. This
        is the accruing value behind it.
        """
        names = list(self.accruals) if markets is None else [m for m in markets if m in self.accruals]
        out = {}
        for market in names:
            est = self.accruals[market].estimate()
            acc = self.accruals[market]
            agg = acc.__dict__.get("_compacted") or {"status": {}}
            idle = sum(1 for mk in acc.marks if mk.status == "idle") + int(agg["status"].get("idle", 0))
            last = next((mk for mk in reversed(acc.marks) if mk.counted), None)
            out[market] = {"raw_usd": Decimal(est.raw_usd), "known": est.known_seconds,
                           "unknown": est.unknown_seconds, "forfeited": est.forfeited_seconds,
                           "idle": idle, "share": None if last is None else last.share}
        return out

    def live_estimates(self, markets=None) -> dict:
        """Payable-USD estimate per market from closed seconds only. Read-only."""
        names = list(self.accruals) if markets is None else [m for m in markets if m in self.accruals]
        out = {}
        for market in names:
            out[market] = Decimal(self.accruals[market].estimate().estimated_usd)
        return out

    def finish(self) -> dict:
        for market, accrual in self.accruals.items():
            open_s = self.open_seconds.get(market)
            if open_s is not None:
                accrual.score_second(open_s)
                self.open_seconds[market] = None
        estimates = {}
        for market, accrual in self.accruals.items():
            est = accrual.estimate()
            estimates[market] = Decimal(est.estimated_usd)
        accepted, rejected = credits_from_ledger(self.ledger)
        shares = {market: amount for market, amount in estimates.items()}
        series_by = {market: prog.series for market, prog in self.programs.items()}
        program_by = {market: market for market in self.programs}
        cash = self.cash or {}
        inferred = infer_reward_credits(
            balance_delta_usd=cash.get("balance_delta_usd", 0),
            fills_cash_usd=cash.get("fills_cash_usd", 0),
            settlements_usd=cash.get("settlements_usd", 0),
            deposits_usd=cash.get("deposits_usd", 0),
            shares=shares,
            series_by_market=series_by,
            program_by_market=program_by,
        )
        matched = reconcile([
            EstimateRow(market, market, self.programs[market].series, amount)
            for market, amount in estimates.items()
        ], accepted)
        observations = [
            RatioObs(row.series, Decimal(row.estimated_usd), Decimal(row.paid_usd), inferred=False)
            for row in matched["matches"] if row.ratio is not None
        ]
        for credit in inferred["credits"]:
            estimated = estimates.get(credit["market"], Decimal(0))
            observations.append(RatioObs(
                credit["series"], estimated, Decimal(credit["amount_usd"]), inferred=True,
            ))
        factors = series_factors(observations)
        samples = []
        for market, amount in estimates.items():
            series = self.programs[market].series
            samples.append(MarketSample(
                market=market,
                observed_per_dollar=float(factors.get(series, 1.0)),
                prior_per_dollar=1.0,
                n=1 if series in factors else 0,
                previous_usd=0.0,
                venue="kalshi",
                series=series,
            ))
        next_usd = reallocate(
            samples, equity=self.bankroll, peak=self.bankroll, fraction=0.25, min_sample=5,
        )
        estimated = sum(estimates.values(), Decimal(0))
        premium = sum(
            (Decimal(str(fill["count"])) * Decimal(int(fill["price_cents"])) / Decimal(100)
             for fill in self.fills),
            Decimal(0),
        )
        rewards = Decimal(inferred["residual_usd"])
        if rewards < 0:
            rewards = Decimal(0)
        day = datetime.fromtimestamp(self.now or 0, timezone.utc).date().isoformat()
        from mm.venues.readonly import book_source
        books = book_source(force_demo=self.mode != "paper")
        return {
            "paper": self.mode == "paper",
            "demo": self.mode == "demo",
            "mode": self.mode,
            "live_armed": False,
            "socket_opened": self.socket_opened,
            "stage": "risk",
            "markets": list(self.programs),
            "selection_count": self.selection_count,
            "excluded": self.excluded,
            "quotes": self.quotes,
            "resting": list(self.resting),
            "cancelled": self.cancels,
            "fills": self.fills,
            "fills_n": len(self.fills),
            "estimated_usd": format(estimated, "f"),
            "estimates": {market: format(amount, "f") for market, amount in estimates.items()},
            "inferred": inferred,
            "calibration_inferred": any(obs.inferred for obs in observations),
            "factors": factors,
            "reconcile": {
                "matches": [row.ratio for row in matched["matches"]],
                "rejected": len(rejected),
            },
            "next_usd": next_usd,
            "risk": self.risk_rows,
            "kill": self.kill,
            "pnl_usd": format(-premium, "f"),
            "rewards_usd": format(rewards, "f"),
            "day": day,
            "data_source": books["flag"],
        }


def load_stream(path: str | Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def run_recorded(path: str | Path, **kwargs) -> dict:
    """Replay a recorded websocket stream. Does not open a socket."""
    loop = RunLoop(**kwargs)
    for row in load_stream(path):
        loop.on_frame(row)
    report = loop.finish()
    report["socket_opened"] = False
    return report


def fetch_demo_programs(host: str) -> list[dict]:
    """Public incentive list on a demo host. Production is refused."""
    if host not in DEMO_HOSTS:
        raise UnattendedRefused(f"program fetch requires a demo host, got {host}")
    import urllib.request
    from engine.lip_discovery import _parse_program
    url = f"https://{host}/trade-api/v2/incentive_programs?status=active&type=liquidity"
    with urllib.request.urlopen(url, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    frames = []
    now = time.time()
    for raw in payload.get("incentive_programs") or []:
        parsed = _parse_program(raw)
        if not parsed:
            continue
        start = datetime.fromisoformat(str(parsed["start_date"]).replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(str(parsed["end_date"]).replace("Z", "+00:00")).timestamp()
        frames.append({
            "kind": "program",
            "market": parsed["market_ticker"],
            "series": parsed["series_ticker"],
            "program_id": parsed.get("id") or parsed["market_ticker"],
            "period_reward_usd": parsed["period_reward_usd"],
            "period_seconds": parsed["period_seconds"],
            "discount_factor": parsed["discount_factor"],
            "target_size": parsed["target_size"],
            "start_ts": start,
            "end_ts": end,
            "close_ts": end,
            "days_to_settle": max(0.0, (end - now) / 86400.0),
        })
    return frames


async def drive_socket(ws_url: str, on_frame: Callable[[dict], None]) -> None:
    """Open the demo websocket and feed frames into ``on_frame``."""
    from execution.kalshi_ws import KalshiWS
    url = resolve_ws_url(ws_url)
    host = assert_demo_host(url)
    programs = fetch_demo_programs(host)
    for frame in programs:
        on_frame(frame)
    ws = KalshiWS(url=url)
    if (urlsplit(ws.url).hostname or "").lower() in PRODUCTION_HOSTS:
        raise UnattendedRefused("production host refused")
    await ws.connect()
    try:
        tickers = [frame["market"] for frame in programs]
        if tickers:
            await ws.subscribe_orderbook(tickers)
            cmd = {
                "id": 9001, "cmd": "subscribe",
                "params": {"channels": ["trade"], "market_tickers": tickers},
            }
            await ws._ws.send(json.dumps(cmd))
        async for raw in ws._ws:
            msg = json.loads(raw)
            msg.setdefault("ts", time.time())
            on_frame(msg)
    finally:
        await ws.close()


READONLY_BACKOFF_START_S = 5.0
READONLY_BACKOFF_MAX_S = 120.0
PROGRAM_MAX_PAGES = 50
CARRY_FORWARD_MAX_S = 900
CAP_REASONS = ("per_market", "per_series", "per_underlying", "per_venue", "gross")


def alloc_cap_fraction() -> float:
    """Allocation stops at this fraction of the risk venue/gross cap (LIP_ALLOC_CAP_FRACTION, 0.95)."""
    try:
        return float(os.environ.get("LIP_ALLOC_CAP_FRACTION", 0.95))
    except (TypeError, ValueError):
        return 0.95


def suspect_per_100() -> float:
    """Plan $/day per $100 capital above this is flagged (LIP_SUSPECT_PER_100, default 40)."""
    try:
        return float(os.environ.get("LIP_SUSPECT_PER_100", 40))
    except (TypeError, ValueError):
        return 40.0
PROGRAM_REFRESH_S = 600.0
MARKET_LOOKUP_PAUSE_S = 0.12


def readonly_transient_types() -> tuple:
    """Errors on the read-only book path that are retried, not fatal.

    HTTP 4xx/5xx from an allowed GET (``ReadOnlyHTTPError``), network
    errors, timeouts, and websocket handshake/close errors. A plain
    ``ReadOnlyViolation`` (write verb, order/portfolio route, private
    channel, non-production host, missing session) is not in this tuple
    and still ends the process.
    """
    import asyncio
    from mm.venues.readonly import ReadOnlyHTTPError
    kinds: list = [ReadOnlyHTTPError, requests.RequestException, OSError,
                   asyncio.TimeoutError, ConnectionError]
    try:
        from websockets.exceptions import WebSocketException
        kinds.append(WebSocketException)
    except Exception:
        pass
    return tuple(kinds)


def fetch_all_programs(reader, *, max_pages: int = PROGRAM_MAX_PAGES,
                       pause_s: float = MARKET_LOOKUP_PAUSE_S, sleep=time.sleep) -> dict:
    """All active liquidity programs, following ``next_cursor``. Read-only GETs."""
    rows: list = []
    cursor = None
    seen: set = set()
    for _ in range(int(max_pages)):
        params = {"status": "active", "type": "liquidity"}
        if cursor:
            params["cursor"] = cursor
        page = reader.get("/incentive_programs", params=params)
        rows.extend(page.get("incentive_programs") or [])
        cursor = page.get("next_cursor") or None
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
        sleep(pause_s)
    return {"incentive_programs": rows}


class ExchangeIndexCache:
    """``exchange_index`` per ticker from read-only GET /markets/{ticker}.

    Cached for the life of the process. Only tickers without a cached value
    are looked up. Permanently excluded intraday series are not looked up.
    One ticker's HTTP error is counted and retried on the next refresh.
    """

    def __init__(self, *, pause_s: float = MARKET_LOOKUP_PAUSE_S, sleep=time.sleep) -> None:
        self.known: dict[str, int] = {}
        self.pause_s = float(pause_s)
        self.sleep = sleep
        self.lookups = 0
        self.failures = 0
        self.skipped = 0

    @staticmethod
    def _skip(frame: dict) -> bool:
        from mm.session_gates import intraday_reason
        return bool(intraday_reason(str(frame.get("series") or ""), str(frame.get("market") or "")))

    def enrich(self, frames: list[dict], reader) -> list[dict]:
        from urllib.parse import quote
        from mm.venues.kalshi import exchange_index_from_market_payload
        from mm.venues.readonly import ReadOnlyHTTPError
        log = logging.getLogger("lip.readonly")
        for frame in frames:
            ticker = str(frame.get("market") or "")
            if not ticker:
                continue
            if ticker in self.known:
                frame["exchange_index"] = self.known[ticker]
                continue
            if self._skip(frame):
                self.skipped += 1
                continue
            try:
                payload = reader.get(f"/markets/{quote(ticker, safe='')}")
            except ReadOnlyHTTPError as exc:
                self.failures += 1
                log.warning("market lookup %s HTTP %s", ticker, exc.status)
                self.sleep(2.0 if exc.status == 429 else self.pause_s)
                continue
            self.lookups += 1
            idx = exchange_index_from_market_payload(payload)
            if idx is not None:
                self.known[ticker] = int(idx)
                frame["exchange_index"] = int(idx)
            self.sleep(self.pause_s)
        return frames


async def drive_readonly_books(source: dict, on_frame: Callable[[dict], None], *,
                               sleep: Callable | None = None,
                               max_attempts: int | None = None,
                               refresh_s: float = PROGRAM_REFRESH_S,
                               cache: "ExchangeIndexCache | None" = None,
                               meta=None) -> None:
    """Production books and public trades. The reader cannot place an order.

    Startup: load programs, screen them against the on-disk metadata cache,
    connect the websocket for the cached candidates, then fetch missing
    market metadata (GET /markets?tickers=..., batched) and series
    categories in the background and add new candidates. Programs are
    re-checked every ``refresh_s``. Only the top ``LIP_CANDIDATE_TOP``
    candidates are fed to the loop and subscribed. Transient HTTP/network/
    websocket errors back off 5 s doubling to 120 s. Safety refusals
    (plain ``ReadOnlyViolation``) propagate.
    """
    import asyncio
    import threading
    from mm.venues.readonly import load_private_key
    from mm.unattended.screen import MetaCache

    log = logging.getLogger("lip.readonly")
    transient = readonly_transient_types()
    nap = sleep or asyncio.sleep
    key = load_private_key(source["key_path"])
    session = requests.Session()
    ctx = {"cache": cache or ExchangeIndexCache(), "meta": meta or MetaCache().load(),
           "fed": {}, "refresh_s": float(refresh_s), "programs": [],
           "lock": threading.Lock()}
    backoff = READONLY_BACKOFF_START_S
    attempts = 0
    try:
        while True:
            attempts += 1
            state = {"frames": False}
            try:
                await _readonly_books_session(source, key, session, on_frame, state, ctx=ctx)
                return
            except transient as exc:
                if state["frames"]:
                    backoff = READONLY_BACKOFF_START_S
                log.warning(
                    "read-only books transient error (%s: %s); retry in %.0fs",
                    type(exc).__name__, str(exc)[:200], backoff,
                )
                if max_attempts is not None and attempts >= max_attempts:
                    raise
                await nap(backoff)
                backoff = min(backoff * 2.0, READONLY_BACKOFF_MAX_S)
    finally:
        session.close()


def _program_sig(frame: dict) -> tuple:
    return tuple(frame.get(k) for k in (
        "program_id", "start_ts", "end_ts", "period_reward_usd", "period_seconds", "target_size",
        "discount_factor", "fee_type", "fee_multiplier", "category", "close_ts", "occurrence_ts"))


def _feed_programs(frames: list[dict], fed: dict, on_frame: Callable[[dict], None],
                   sigs: dict | None = None) -> list[str]:
    """Feed new programs; send a 'shard' frame when a known one gains an index.

    Patch 21 (audit): a known market whose program changed (new period window,
    pool, target, fee_type, close) is re-fed. Before, a market was fed once per
    process, so a program roll-over left the loop on the expired window
    (seconds_left 0 => never selected again) and late metadata (fee_type)
    never reached it. RunLoop.add_program keeps book/accrual when only
    descriptive fields changed. Returns only markets new to the feed.
    """
    new = []
    for frame in frames:
        market = frame["market"]
        idx = frame.get("exchange_index")
        sig = _program_sig(frame)
        if market not in fed:
            on_frame(frame)
            fed[market] = idx
            new.append(market)
            if sigs is not None:
                sigs[market] = sig
            continue
        if fed[market] is None and idx is not None:
            on_frame({"kind": "shard", "market": market, "exchange_index": idx})
            fed[market] = idx
        if sigs is not None and sigs.get(market) != sig:
            if market in sigs:
                on_frame(frame)
            sigs[market] = sig
    return new


class SequenceGap(ConnectionError):
    """A real gap in one websocket subscription's sequence. Reconnect to resync."""


class SidSequencer:
    """Kalshi ``seq`` is per subscription (sid), shared by every ticker on it.

    Checked once here; per-market books then see ``seq=None`` so they do not
    mistake other tickers' messages for gaps.
    """

    def __init__(self) -> None:
        self.last: dict = {}
        self.gaps = 0
        self.dups = 0

    def check(self, msg: dict) -> str:
        sid = msg.get("sid")
        seq = msg.get("seq")
        if sid is None or seq is None:
            return "ok"
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return "ok"
        last = self.last.get(sid)
        if last is not None and seq <= last:
            self.dups += 1
            return "dup"
        self.last[sid] = seq
        if last is not None and seq > last + 1:
            if msg.get("type") == "orderbook_snapshot":
                # Kalshi merges later subscribe batches into the same sid and
                # skips seq numbers at the added tickers' snapshots. A snapshot
                # fully resets that market's book, so it is a resync, not a gap.
                self.resyncs = getattr(self, "resyncs", 0) + 1
                return "ok"
            self.gaps += 1
            return "gap"
        return "ok"


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def size_ladder(chunk: float = 100.0) -> list[float]:
    """LIP_SIZE_LADDER: comma list of contract sizes per side. Unset = [chunk]."""
    raw = os.environ.get("LIP_SIZE_LADDER", "")
    out = []
    for part in raw.split(","):
        try:
            v = float(part)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return sorted(set(out)) or [float(chunk)]


_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_DATE_TOKEN = re.compile(r"(?<![0-9])(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(?![0-9])")


def ticker_event_day_ts(ticker: str) -> float | None:
    """Earliest YYMONDD token in a ticker (KXTRUMPMENTIONB-26OCT01-CAFE ->
    2026-10-01 00:00 America/New_York). None when the ticker has no day token."""
    from zoneinfo import ZoneInfo
    best = None
    for yy, mon, dd in _DATE_TOKEN.findall(str(ticker).upper()):
        try:
            ts = datetime(2000 + int(yy), _MONTHS[mon], int(dd),
                          tzinfo=ZoneInfo("America/New_York")).timestamp()
        except ValueError:
            continue
        best = ts if best is None else min(best, ts)
    return best


def event_anchor_ts(ticker: str, occurrence_ts: float | None) -> float | None:
    vals = [v for v in (ticker_event_day_ts(ticker), occurrence_ts) if v is not None]
    return min(vals) if vals else None


def event_window_hours() -> float:
    """LIP_EVENT_WINDOW_HOURS: stop quoting this many hours before the event day
    (ticker date token, 00:00 ET) or occurrence time. 0 (default) = off."""
    return _env_num("LIP_EVENT_WINDOW_HOURS", 0.0)


def proven_series() -> set:
    raw = os.environ.get("LIP_EVENT_PROVEN_SERIES", "")
    return {p.strip().upper() for p in raw.split(",") if p.strip()}


def durable_reserve() -> float:
    """LIP_DURABLE_RESERVE: fraction of the allocation budget reserved for the durable bucket."""
    try:
        return min(1.0, max(0.0, float(os.environ.get("LIP_DURABLE_RESERVE", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def durable_min_days() -> float:
    try:
        return float(os.environ.get("LIP_DURABLE_MIN_DAYS", 14.0))
    except (TypeError, ValueError):
        return 14.0


def split_bucket_budgets(budget: float, reserve: float, has_durable: bool, has_short: bool) -> dict:
    """Durable gets ``reserve`` x budget, short the rest. A bucket with no
    positive-rank candidates hands its share to the other bucket."""
    dur = float(budget) * float(reserve)
    short = float(budget) - dur
    if not has_durable:
        short, dur = short + dur, 0.0
    elif not has_short:
        dur, short = dur + short, 0.0
    return {"durable": dur, "short": short}


def rank_min_score() -> float:
    try:
        return float(os.environ.get("LIP_RANK_MIN_SCORE", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _fetch_programs(reader) -> list[dict]:
    return _programs_from_incentive(fetch_all_programs(reader))


def _refresh_meta(reader, ctx: dict) -> None:
    """Thread body: fetch stale market metadata and missing series categories; persist."""
    from mm.unattended.screen import needs_series
    meta = ctx["meta"]
    with ctx["lock"]:
        programs = list(ctx["programs"])
        tickers = [f["market"] for f in programs if f.get("market")]
        stale = meta.stale_markets(tickers)
        if stale:
            meta.fetch_markets(reader, stale)
        missing = needs_series(programs, meta) + meta.stale_series(
            {str(f.get("series") or "") for f in programs if f.get("market") in ctx["fed"]})
        missing = [s for s in dict.fromkeys(missing) if s]
        if missing:
            meta.fetch_series(reader, missing)
        meta.save()


def _screen_and_feed(ctx: dict, on_frame: Callable[[dict], None]) -> list[str]:
    from mm.unattended.screen import screen
    log = logging.getLogger("lip.screen")
    if not ctx["lock"].acquire(blocking=False):
        return []
    try:
        candidates, stats = screen(ctx["programs"], ctx["meta"])
    finally:
        ctx["lock"].release()
    new = _feed_programs(candidates, ctx["fed"], on_frame, ctx.setdefault("sigs", {}))
    stats["fed"] = len(ctx["fed"])
    stats["new"] = len(new)
    on_frame({"kind": "screen", "stats": stats})
    log.info("screen: programs %d eligible %d candidates %d fed %d new %d reasons %s",
             stats["programs_total"], stats["eligible"], stats["candidates"],
             stats["fed"], len(new), stats["reasons"])
    for why in ("sports_match", "sports_short_dated"):
        if stats["samples"].get(why):
            log.info("excluded %s (%d): %s", why, stats["reasons"].get(why, 0),
                     ",".join(stats["samples"][why]))
    return new


async def _subscribe(sock, tickers) -> None:
    from mm.venues.readonly import PUBLIC_WS_CHANNELS
    names = sorted(tickers)
    if not names:  # an empty ticker list must never become a subscribe-to-everything
        return
    for i in range(0, len(names), 100):
        await sock.subscribe(sorted(PUBLIC_WS_CHANNELS), names[i:i + 100])


async def _background(reader, ctx: dict, on_frame, sock, state: dict) -> None:
    import asyncio
    log = logging.getLogger("lip.readonly")
    transient = readonly_transient_types()
    first = True
    while True:
        if not first:
            await asyncio.sleep(ctx["refresh_s"])
        try:
            if not first or not ctx["programs"]:
                ctx["programs"] = await asyncio.to_thread(_fetch_programs, reader)
            first = False
            t0 = time.time()
            await asyncio.to_thread(_refresh_meta, reader, ctx)
            new = _screen_and_feed(ctx, on_frame)
            if new:
                await _subscribe(sock, new)
            log.info("background refresh %.1fs; subscribed %d new", time.time() - t0, len(new))
        except asyncio.CancelledError:
            raise
        except transient as exc:
            first = False
            log.warning("background refresh transient error (%s: %s)", type(exc).__name__, str(exc)[:200])
        except BaseException as exc:  # safety refusal: stop the session
            state["fatal"] = exc
            try:
                await sock.close()
            except Exception:
                pass
            return


async def _readonly_books_session(source: dict, key, session,
                                  on_frame: Callable[[dict], None], state: dict,
                                  ctx: dict | None = None) -> None:
    import asyncio
    import threading
    from mm.venues.readonly import ReadOnlyKalshiTransport, ReadOnlyMarketSocket
    from mm.unattended.screen import MetaCache
    if ctx is None:
        ctx = {"cache": ExchangeIndexCache(), "meta": MetaCache().load(), "fed": {},
               "refresh_s": PROGRAM_REFRESH_S, "programs": [], "lock": threading.Lock()}
    reader = ReadOnlyKalshiTransport(api_key=source["key_id"], private_key=key, session=session)
    if not ctx["programs"]:
        ctx["programs"] = await asyncio.to_thread(_fetch_programs, reader)
    if not ctx["fed"]:
        _screen_and_feed(ctx, on_frame)
    sock = ReadOnlyMarketSocket(api_key=source["key_id"], private_key=key, url=source["ws_url"])
    task = None
    try:
        await sock.connect()
        await _subscribe(sock, ctx["fed"])
        task = asyncio.create_task(_background(reader, ctx, on_frame, sock, state))
        seqr = SidSequencer()
        try:
            async for raw in sock._ws:
                state["frames"] = True
                msg = json.loads(raw)
                msg.setdefault("ts", time.time())
                kind = str(msg.get("type") or "")
                if kind == "trade":
                    body = msg.get("msg") or msg
                    on_frame({"type": "trade", "ts": msg["ts"], "trade": body})
                elif kind in ("orderbook_snapshot", "orderbook_delta"):
                    verdict = seqr.check(msg)
                    if verdict == "dup":
                        continue
                    if verdict == "gap":
                        raise SequenceGap(f"sid {msg.get('sid')} sequence gap at {msg.get('seq')}")
                    msg["seq"] = None
                    on_frame(msg)
        except BaseException:
            if state.get("fatal") is not None:
                raise state["fatal"]
            raise
        if state.get("fatal") is not None:
            raise state["fatal"]
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        try:
            await sock.close()
        except Exception:
            pass


def _programs_from_incentive(payload: dict) -> list[dict]:
    from engine.lip_discovery import _parse_program
    frames = []
    now = time.time()
    for raw in payload.get("incentive_programs") or []:
        parsed = _parse_program(raw)
        if not parsed:
            continue
        start = datetime.fromisoformat(str(parsed["start_date"]).replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(str(parsed["end_date"]).replace("Z", "+00:00")).timestamp()
        frames.append({
            "kind": "program",
            "market": parsed["market_ticker"],
            "series": parsed["series_ticker"],
            "program_id": parsed.get("id") or parsed["market_ticker"],
            "period_reward_usd": parsed["period_reward_usd"],
            "period_seconds": parsed["period_seconds"],
            "discount_factor": parsed["discount_factor"],
            "target_size": parsed["target_size"],
            "start_ts": start,
            "end_ts": end,
            "close_ts": end,
            "days_to_settle": max(0.0, (end - now) / 86400.0),
        })
    return frames


def waiting_report(ws_url: str) -> dict:
    from mm.venues.readonly import book_source
    books = book_source()
    return {
        "paper": True,
        "demo": False,
        "mode": "paper",
        "live_armed": False,
        "socket_opened": False,
        "stage": "waiting_for_demo_key",
        "markets": [],
        "quotes": [],
        "fills_n": 0,
        "estimated_usd": "0",
        "pnl_usd": "0",
        "rewards_usd": "0",
        "day": datetime.now(timezone.utc).date().isoformat(),
        "kill": None,
        "ws_url": ws_url,
        "data_source": books["flag"],
    }
