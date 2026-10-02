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
    clamp_contracts, inside_close_window, max_contracts_for_fill, pull_before_close_s,
    single_fill_cap_usd,
)
from mm.unattended.feed import DEMO_WS_URL
from mm.unattended.optimize import optimize_sizes
from mm.unattended.screen import RANK_PENALTY_UNIT
from mm.unattended.service import UnattendedRefused
from mm.venues.kalshi_rest import DEMO_HOSTS, PRODUCTION_HOSTS

SELECT_EVERY_S = 600.0
# Default LIP_CLOCK_SKEW_LIMIT_S for the loop's skew guard (mm.ops.SKEW_LIMIT_S,
# 2 s, pulled quotes on ordinary network jitter).
CLOCK_SKEW_LIMIT_S = 5.0


def _flag(env: dict, name: str, default: str) -> bool:
    return str(env.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def resolve_mode(environ: dict | None = None) -> str:
    """Paper, else demo, else refuse. Live arming flags are not consulted.

    ``LIP_FORCE_PAPER`` (truthy) allows paper only: with it set, a false
    ``LIP_PAPER`` is refused instead of falling through to demo. systemd's
    EnvironmentFile= overrides the unit's Environment=, so ``LIP_PAPER=false``
    in /etc/lip-maker/lip-maker.env would otherwise silently change the mode.
    """
    env = os.environ if environ is None else environ
    paper = _flag(env, "LIP_PAPER", "true")
    if _flag(env, "LIP_FORCE_PAPER", "false"):
        if not paper:
            raise UnattendedRefused("LIP_FORCE_PAPER is set and LIP_PAPER is not true: paper only")
        return "paper"
    if paper:
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
    # A missing fee_type is charged as the standard maker fee (fail conservative).
    fee_type: str = "quadratic_with_maker_fees"
    fee_multiplier: float = 1.0
    program_id: str = ""
    # max_reward_per_account in dollars (None: the program sets no account cap).
    max_reward_usd: float | None = None


VENUES = ("kalshi", "pmus")
STATE_VERSION = 1
# Bounded history for the long-running live loop (counts stay exact).
LIST_CAP = 20000


def rank_live_score(net_per_day: float, penalty_per_day: float, capital_usd: float) -> float:
    """Final allocation rank: (plan net $/day - markout penalty $/day) / $ capital.

    ``penalty_per_day`` must already be at the size ``net_per_day`` was
    evaluated at (the program's rank_penalty_per_day is per 100 contracts)."""
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
        # fill_cap bounds every resting order (one fill can lose at most this).
        # The eligibility pre-passes (fast_allocate/allocate/optimize_sizes)
        # evaluate economics at one probe size and exclude a market whose probe
        # exceeds their cap, so they get the configured cap; the order clamp
        # in _quote/_size_curve applies fill_cap.
        self.fill_cap = default_fill_cap_usd() if fill_cap is None else float(fill_cap)
        self.screen_fill_cap = single_fill_cap_usd() if fill_cap is None else float(fill_cap)
        self.sim = PaperFillSimulator(latency_ms=latency_ms)
        from config.settings import RAMP_PHASE
        self.risk = RiskEngine(
            limits=Limits.from_capital(Decimal(str(self.bankroll)), ramp=RAMP_PHASE),
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
        # FV-driven paper quoting (LIP_FV_QUOTE_ENABLE): strike hints per
        # market, markets selected only because a fair value let them past the
        # close-time gate (pulled when it goes away), and counters.
        self._fv_hints: dict = {}
        self._fv_admitted: set = set()
        self.fv_quote_stats: dict = {}
        # Out-of-sample scoring of model fair values vs the book (measurement).
        from mm.unattended.fv_calib import FVCalibration
        self.fv_calib = FVCalibration()
        self._fv_calib_at = 0.0
        # Calibration watch: every fed market of a model-priced FV family
        # stays a calibration target until its close, whether or not it is
        # quoted and even after its program left the loop (and across a
        # restart: persisted). market -> {series, close_ts, added_ts}.
        # ``fv_calib_view`` is its key set for the fair-value cache thread
        # (an immutable frozenset, replaced whole).
        self.fv_calib_watch: dict[str, dict] = {}
        self.fv_calib_view: frozenset = frozenset()
        # Kalshi markets with pending calibration samples past close +
        # LIP_FV_CALIB_BACKFILL_AFTER_S: also asked by the REST settlement
        # backfill (an immutable tuple, replaced whole; read by its thread).
        self.fv_settle_view: tuple = ()
        self._fv_settle_at = 0.0
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
        # Rolled-over program periods: market -> raw reward USD (capped at
        # max_reward per period) of windows that ended while we were running.
        self.closed_periods: dict[str, float] = {}
        self.closed_periods_n = 0
        # Closed periods of ended markets, folded per "venue/bucket" (bucket
        # "" when unknown) so the persisted map does not grow per market.
        self.closed_periods_agg: dict[str, float] = {}
        # Settled positions dropped after LIP_SETTLED_KEEP_DAYS: realized
        # markout (all positions) and per-bucket aggregates of their rows.
        self.realized_pruned_usd = 0.0
        self.bucket_closed: dict[str, dict] = {}
        # Lifetime settled positions (held legs at settlement) per venue and
        # their realized settlement P&L (payout - cost basis, fees excluded).
        # Unlike ``settled`` these are never pruned. ``lower_bound``: seeded
        # from an older state file that had no counters (positions pruned
        # before then are not counted; the USD total includes them).
        self.settled_lifetime: dict = {"by_venue": {}, "total_usd": 0.0, "lower_bound": False}
        self.refeeds_n = 0
        # Review fixes: fill aggregates (self.fills is a bounded list), MTM
        # marks, fees, settlement, inventory on the risk engine, feed state.
        self.fills_total = 0
        self.fills_by_venue: dict[str, int] = {}
        # Synthetic (model-inferred, low-fidelity) paper fills per venue: PM
        # US prints inferred from two book polls and cross-fills on polled
        # books. Included in fills_by_venue too.
        self.fills_synthetic_by_venue: dict[str, int] = {}
        self.premium_usd_total = 0.0
        self.fees_usd_total = 0.0
        self.bucket_pos: dict[str, dict] = {}
        self.last_mid: dict[str, float] = {}
        self.settled: dict[str, dict] = {}
        self.inv_committed: dict[str, Decimal] = {}
        self.connected = True
        self.disconnected_at: float | None = None
        self.disconnects_n = 0
        self.last_reconnect: dict | None = None
        self._reselect_pending = False
        self.alerts: list[dict] = []
        self.skew_n = 0
        self.cap_trims_n = 0
        self.programs_pruned_n = 0
        self._pnl_day: dict | None = None
        # Settlement backstops (F2): held, unsettled positions due a
        # settlement check (read by the Kalshi background refresh and the PM
        # US poller threads: an immutable tuple of (market, venue), replaced
        # whole), PM US positions released as unresolved, alerted markets.
        self.settle_view: tuple = ()
        self.unresolved: dict[str, dict] = {}
        self._unsettled_alerted: set = set()
        self._settle_tick: int | None = None
        # Clock-skew guard (_note_clock_skew).
        self._skew_streak = 0
        self._skew_since = 0.0
        self._skew_active = False
        # Phase 4: multi-horizon fill markouts (measurement only).
        from mm.unattended.markouts import MarkoutBook
        self.markouts = MarkoutBook()
        # Phase 4: operator-maintained scheduled-event calendar
        # (LIP_EVENT_CALENDAR_FILE; unset = no pulls).
        from config.event_calendar import EventCalendar
        self.calendar = EventCalendar.from_env()
        self._calendar_alerted: str | None = None
        self.state_path: str | None = None
        self.state_error: str | None = None
        self._state_dirty = False
        self._state_saved_at = 0.0
        # Frame thread and the service's 1 s timer thread both take this.
        import threading as _threading
        self.lock = _threading.RLock()

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
            fee_type=str(row.get("fee_type") or "quadratic_with_maker_fees"),
            fee_multiplier=float(row.get("fee_multiplier") if row.get("fee_multiplier") is not None else 1.0),
            program_id=str(row.get("program_id") or market),
            max_reward_usd=None if row.get("max_reward_usd") is None else float(row["max_reward_usd"]),
        )
        if prog.venue not in VENUES:
            raise ValueError(f"unknown venue {prog.venue!r}")
        self._fv_note_program(market, prog, row)
        if old_prog is not None and old_acc is not None and (
                old_prog.program_id, old_prog.start_ts, old_prog.end_ts, old_prog.period_reward_usd,
                old_prog.target_size, old_prog.discount_factor, old_prog.max_spread_usd) == (
                prog.program_id, prog.start_ts, prog.end_ts, prog.period_reward_usd,
                prog.target_size, prog.discount_factor, prog.max_spread_usd):
            # Same program window re-fed (e.g. refreshed metadata): keep the
            # accrual and book, update the descriptive fields and the cap.
            self.programs[market] = prog
            old_acc.max_reward_usd = (None if prog.max_reward_usd is None
                                      else Decimal(str(prog.max_reward_usd)))
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
        acc = SecondAccrual(params, series=prog.series,
                            max_reward_usd=(None if prog.max_reward_usd is None
                                            else Decimal(str(prog.max_reward_usd))))
        if old_acc is not None:
            # Patch 21: a new program window for a known market (period
            # roll-over). Keep the live book (the WS sends a snapshot only on
            # subscribe) and our resting orders; archive the old raw accrual
            # (capped at that window's max_reward) so status totals do not
            # reset at the roll.
            acc.book = old_acc.book
            acc.resting = list(old_acc.resting)
            self._archive_period(market, old_acc)
            self.refeeds_n += 1
        self.accruals[market] = acc
        self.open_seconds.setdefault(market, None)

    def on_frame(self, row: dict) -> None:
        kind = str(row.get("kind") or row.get("type") or "")
        if kind == WS_RAW_TYPE:
            return  # raw websocket evidence for the recorder only (_dispatch_ws_message)
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
        if kind == "disconnect":
            self.note_disconnect(str(row.get("reason") or "disconnect"))
            return
        if kind == "reconnect":
            self.note_reconnect(float(row.get("stale_s") or 0.0))
            return
        if kind == "program_end":
            self.end_program(str(row.get("market") or ""), str(row.get("reason") or "program_end"))
            return
        if kind == "settlement":
            self.settle(str(row.get("market") or ""), str(row.get("result") or ""),
                        source=str(row.get("source") or "ws_lifecycle"))
            return
        ts = float(row.get("ts") if row.get("ts") is not None else self.now)
        self.now = ts
        if kind in ("orderbook_snapshot", "orderbook_delta", "trade", "clock"):
            self._check_settlements(ts)
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
            # Carry forward only while the feed is connected: a quiet book
            # means "unchanged" only when messages could have arrived.
            if (self.carry_forward and self.connected and market in self.resting
                    and accrual._next is not None
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
        accrual.on_message(row, ts)
        self.open_seconds[market] = int(ts)
        # PM US books are polled through a cache: ``data_ts`` is the data
        # time the poller derived (older than the receive time), and
        # drain_external clamps ``ts`` to the loop clock, so the book's age
        # for _book_fresh comes from ``data_ts``, never later than ``ts``.
        data_ts = row.get("data_ts") if self._venue(market) == "pmus" else None
        self._book_ts[market] = ts if data_ts is None else min(ts, float(data_ts))
        self._note_mark(market)
        exchange_ts = row.get("exchange_ts")
        if exchange_ts is not None:
            self._note_clock_skew(ts, float(exchange_ts))

    def _note_clock_skew(self, ts: float, exchange_ts: float) -> None:
        """Clock-skew guard on Kalshi book frames (snapshots and deltas).

        ``exchange_ts`` is Kalshi's ``sending_ts_ms`` ("when Kalshi queued
        this message at the network layer", on both orderbook_snapshot and
        orderbook_delta; docs.kalshi.com/websockets/orderbook-updates), else
        a delta's ``ts_ms`` (when the change was recorded). Both are send /
        change times, never a "last change" time of an unchanged book, so a
        snapshot's age is real lag and the guard applies to both kinds.
        Frames without it (replay, PM US polls) neither count nor reset.

        Every frame is applied (the book stays in sequence). One late frame
        is noise; skew beyond LIP_CLOCK_SKEW_LIMIT_S (5 s) on
        LIP_CLOCK_SKEW_N (3) consecutive frames, or on two or more
        consecutive frames spanning LIP_CLOCK_SKEW_SUSTAIN_S (3 s), means the
        local clock or processing is behind: every resting quote is pulled
        (the lag is process-wide) and selection is held. The first clean
        frame clears it and re-selects (re-quotes) at once."""
        limit = _env_num("LIP_CLOCK_SKEW_LIMIT_S", CLOCK_SKEW_LIMIT_S)
        if not skew_is_excessive(ts, exchange_ts, limit_s=limit):
            self._skew_streak = 0
            if self._skew_active:
                self._skew_active = False
                self._reselect_pending = True
                logging.getLogger("lip.risk").warning("clock skew cleared: re-selecting")
            return
        self.skew_n += 1
        if self._skew_streak == 0:
            self._skew_since = ts
        self._skew_streak += 1
        if self._skew_active:
            return
        need = max(1, int(_env_num("LIP_CLOCK_SKEW_N", 3)))
        sustain = _env_num("LIP_CLOCK_SKEW_SUSTAIN_S", 3.0)
        if self._skew_streak >= need or (self._skew_streak >= 2 and ts - self._skew_since >= sustain):
            self._skew_active = True
            n = len(self.resting)
            if n:
                self.pulls["clock_skew"] = self.pulls.get("clock_skew", 0) + n
                self._cancel_all("clock_skew")
            logging.getLogger("lip.risk").warning(
                "clock skew %.1fs > %.1fs on %d frames: %d quotes pulled until a clean frame",
                ts - exchange_ts, limit, self._skew_streak, n)

    # ------------------------------------------------------------ feed state
    def note_disconnect(self, reason: str = "disconnect") -> None:
        """The websocket dropped (transient error, sequence gap, clean close).

        Seconds up to the last frame are closed against the book as it was.
        Then every book is marked disconnected (stale until its own next
        snapshot) and resting quotes are pulled (paper cannot see the trades
        that would have filled them while the feed is down), so outage
        seconds are never carried forward or scored: they are unknown (stale
        book) or idle (no quote)."""
        if self.connected and self.now:
            self._close_elapsed(int(self.now) + 1)
        for accrual in self.accruals.values():
            accrual.book.note_disconnect()
        if self.connected:
            self.disconnects_n += 1
            self.disconnected_at = self.now or None
            logging.getLogger("lip.risk").warning(
                "feed disconnected (%s): books marked stale, quotes pulled", reason)
        self.connected = False
        self._cancel_all(f"disconnect:{reason}")
        self._state_dirty = True

    def note_reconnect(self, stale_s: float) -> None:
        """The feed is back after ``stale_s`` seconds without data.

        ``RiskEngine.on_reconnect`` decides: within grace nothing, past
        DISCONNECT_PULL_SEC pull every quote, past LIP_DISCONNECT_KILL_S latch
        the kill (``on_disconnect``). Books stay stale until their own snapshot
        arrives; a fresh selection runs once enough books are usable again."""
        decision = self.risk.on_reconnect(float(stale_s))
        self.connected = True
        self.last_reconnect = {"ts": self.now, "stale_s": round(float(stale_s), 1),
                               "decision": decision.reason}
        if not decision.allowed:
            if self.risk.killed:
                self._latch_kill(decision.reason, cancel_all=True)
            else:
                self._cancel_all(decision.reason)
        self._reselect_pending = True
        self._state_dirty = True

    def end_program(self, market: str, reason: str = "program_end") -> None:
        """Forget an ended program: archive its accrual, cancel its quote, drop
        its book and per-market state. A held position and its last mark stay
        (inventory is only released by settlement)."""
        if market not in self.programs:
            return
        self._cancel(market, reason)
        pos = self.position.get(market)
        if pos is not None:
            # Keep when it should settle: its close, else now (program gone).
            prog = self.programs[market]
            if prog.close_ts is not None:
                pos["close_ts"] = float(prog.close_ts)
            elif pos.get("close_ts") is None:
                pos["orphan_ts"] = float(self.now or time.time())
            self._state_dirty = True
        acc = self.accruals.pop(market, None)
        if acc is not None:
            self._archive_period(market, acc)
        self._fold_closed_period(market, self.programs[market].venue)
        self.programs.pop(market, None)
        for table in (self.open_seconds, self._book_ts, self.last_plan, self._repeg_at, self._fv_state):
            table.pop(market, None)
        self._fv_wanted.discard(market)
        self._fv_admitted.discard(market)
        self._fv_hints.pop(market, None)
        self.quoted_ever.discard(market)
        for key in [k for k in self.cooldown if k[0] == market]:
            del self.cooldown[key]
        if market not in self.position:
            self.last_mid.pop(market, None)
            (getattr(self, "bucket_of", None) or {}).pop(market, None)
        self.programs_pruned_n += 1

    def prune_ended(self, ts: float) -> int:
        """End programs whose window closed more than LIP_PROGRAM_PRUNE_S
        (6 h) ago and were not re-fed with a new window. Bounds memory for
        feeds that never send ``program_end`` (replay, PM US)."""
        grace = _env_num("LIP_PROGRAM_PRUNE_S", 6 * 3600.0)
        gone = [m for m, p in self.programs.items() if p.end_ts + grace < ts]
        for market in gone:
            self.end_program(market, "program_ended")
        self._fv_wanted &= set(self.programs)
        if self.fv_calib.prune(ts):
            self._state_dirty = True
        # closed periods restored for markets that are no longer fed
        for market in [m for m in self.closed_periods if m not in self.programs]:
            self._fold_closed_period(market, self._venue_of(market))
        self._drop_settled(ts)
        return len(gone)

    def _fold_closed_period(self, market: str, venue: str) -> None:
        """Move an ended market's closed-period rewards into the
        ``venue/bucket`` aggregate (status totals unchanged)."""
        usd = self.closed_periods.pop(market, None)
        if usd is None:
            return
        bucket = (getattr(self, "bucket_of", None) or {}).get(market) or ""
        key = f"{venue}/{bucket}"
        self.closed_periods_agg[key] = self.closed_periods_agg.get(key, 0.0) + float(usd)
        self._state_dirty = True

    def _drop_settled(self, ts: float) -> None:
        """Forget positions settled more than LIP_SETTLED_KEEP_DAYS (7) ago
        (and settled rows with no position). Their realized markout, fees,
        rebates, premium and fill counts move into ``realized_pruned_usd`` /
        ``bucket_closed``, so P&L and bucket totals do not change."""
        keep = _env_num("LIP_SETTLED_KEEP_DAYS", 7.0) * 86400.0
        old = [m for m, row in self.settled.items()
               if row.get("ts") is not None and float(row["ts"]) + keep < ts and m not in self.programs]
        for market in old:
            pos = self.position.pop(market, None)
            mark = 100.0 if self.settled[market]["result"] == "yes" else 0.0
            if pos is not None:
                value = (float(pos["yes"]) * mark + float(pos["no"]) * (100.0 - mark)) / 100.0
                self.realized_pruned_usd += value - float(pos["yes_cost"]) - float(pos["no_cost"])
            for bucket, rows in self.bucket_pos.items():
                row = rows.pop(market, None)
                if row is None:
                    continue
                cost = float(row["yes_cost"]) + float(row["no_cost"])
                value = (float(row["yes"]) * mark + float(row["no"]) * (100.0 - mark)) / 100.0
                agg = self.bucket_closed.setdefault(bucket, {"markout_usd": 0.0, "fees_usd": 0.0,
                                                             "rebates_usd": 0.0, "premium_usd": 0.0,
                                                             "fills_n": 0.0, "synthetic_n": 0.0})
                agg["markout_usd"] += value - cost
                agg["fees_usd"] += float(row.get("fees", 0.0))
                agg["rebates_usd"] += float(row.get("rebates", 0.0))
                agg["premium_usd"] += cost
                agg["fills_n"] += float(row.get("fills_n", 0.0))
                agg["synthetic_n"] += float(row.get("synthetic_n", 0.0))
            del self.settled[market]
            self.inv_committed.pop(market, None)
            self.unresolved.pop(market, None)
            self.last_mid.pop(market, None)
            (getattr(self, "bucket_of", None) or {}).pop(market, None)
            self._unsettled_alerted.discard(market)
        if old:
            self._daily_cache = None
            self._state_dirty = True

    def settle(self, market: str, result: str, *, source: str = "ws_lifecycle") -> None:
        """Book settlement of a held position (YES pays 100c on "yes", NO on
        "no") and release its locked capital on the risk engine. Fed by the
        read-only socket's market_lifecycle_v2 channel (determined/settled
        events), the Kalshi REST backfill (``_settlement_backfill``) and the
        PM US gateway settlement poll (``PMUSFeed.poll_settlements``); the
        source is kept on the settled row. The lifecycle channel carries
        every Kalshi market, so a market with neither a position nor a
        program here is ignored (nothing to settle, and nothing is stored for
        it). A position whose result never arrives stays marked at its last
        mark and is listed in status (see ``_check_settlements``). A market
        with recorded model fair values is scored first (``fv_calib``)."""
        result = str(result).lower()
        if result not in ("yes", "no") or market in self.settled:
            return
        if self.fv_calib.on_settle(market, result):
            # Scored even with nothing held (fv_calib: model vs book).
            self._state_dirty = True
        if market not in self.position and market not in self.programs:
            return
        self.unresolved.pop(market, None)
        self._count_settled(market, result)
        legs = [(str((self.position.get(market) or {}).get("venue") or self._venue_of(market)), b,
                 rows[market]["yes"], rows[market]["no"], rows[market]["yes_cost"], rows[market]["no_cost"],
                 rows[market].get("fills_n", 0))
                for b, rows in self.bucket_pos.items() if market in rows]
        self.markouts.on_settle(market, result, legs)
        self.settled[market] = {"result": result, "ts": self.now, "source": str(source)}
        self.last_mid[market] = 100.0 if result == "yes" else 0.0
        self._sync_inventory(market)
        self._cancel(market, "settled")
        self._state_dirty = True

    @staticmethod
    def _settle_value_usd(pos: dict, result: str) -> float:
        """Payout of a position's legs at ``result`` minus their cost basis."""
        mark = 100.0 if result == "yes" else 0.0
        value = (float(pos["yes"]) * mark + float(pos["no"]) * (100.0 - mark)) / 100.0
        return value - float(pos["yes_cost"]) - float(pos["no_cost"])

    def _count_settled(self, market: str, result: str) -> None:
        """Lifetime counters (``settled_lifetime``) for a held position that
        settles now. Called once per market (settle() returns early for a
        market already in ``settled``)."""
        pos = self.position.get(market)
        if not pos or (float(pos.get("yes") or 0.0) <= 0.0 and float(pos.get("no") or 0.0) <= 0.0):
            return
        venue = str(pos.get("venue") or self._venue_of(market))
        by = self.settled_lifetime["by_venue"]
        by[venue] = int(by.get(venue, 0)) + 1
        self.settled_lifetime["total_usd"] = (float(self.settled_lifetime["total_usd"])
                                              + self._settle_value_usd(pos, result))

    def settled_report(self) -> dict:
        """Status: lifetime settled positions (never pruned) and their
        realized settlement P&L (payout - cost basis, fees excluded)."""
        by = {k: int(v) for k, v in sorted(self.settled_lifetime["by_venue"].items())}
        return {"settled_positions_n": sum(by.values()),
                "settled_positions_by_venue": by,
                "settled_total_usd": round(float(self.settled_lifetime["total_usd"]), 6),
                "settled_positions_lower_bound": bool(self.settled_lifetime.get("lower_bound"))}

    # ------------------------------------------------------------ settlement backstops
    def _close_of(self, market: str):
        """When a held position should have settled: its program's close,
        else the close remembered on the position, else (no program left and
        no close known) when the program went away. None while a program
        with no close is live."""
        prog = self.programs.get(market)
        if prog is not None and prog.close_ts is not None:
            return float(prog.close_ts)
        pos = self.position.get(market) or {}
        if pos.get("close_ts") is not None:
            return float(pos["close_ts"])
        if prog is not None:
            return None
        return None if pos.get("orphan_ts") is None else float(pos["orphan_ts"])

    def _check_settlements(self, ts: float) -> None:
        """Once per loop second: refresh ``settle_view`` (held, unsettled
        positions past close or with no program left: the Kalshi REST
        backfill and the PM US settlement poll ask about these), alert once
        for a position still unsettled LIP_UNSETTLED_ALERT_S (24 h) past
        close, and release a PM US position's capital after close +
        LIP_PMUS_UNSETTLED_RELEASE_S (24 h): it leaves inventory, budgets
        and the risk caps, while P&L keeps it at a loss of its full cost
        basis and status lists it in ``unresolved_positions`` until a
        settlement arrives. Kalshi positions are not released (their REST
        backfill settles them; the alert flags one that it cannot)."""
        sec = int(ts)
        if self._settle_tick == sec:
            return
        self._settle_tick = sec
        view = []
        alert_s = _env_num("LIP_UNSETTLED_ALERT_S", 86400.0)
        release_s = _env_num("LIP_PMUS_UNSETTLED_RELEASE_S", 86400.0)
        for market, pos in self.position.items():
            if market in self.settled:
                continue
            if market not in self.programs and pos.get("close_ts") is None and pos.get("orphan_ts") is None:
                pos["orphan_ts"] = float(ts)  # restored without a close: count from now
                self._state_dirty = True
            close = self._close_of(market)
            venue = str(pos.get("venue") or self._venue_of(market))
            if market not in self.programs or (close is not None and ts >= close):
                view.append((market, venue))
            if close is None:
                continue
            if ts >= close + alert_s and market not in self._unsettled_alerted:
                self._unsettled_alerted.add(market)
                self._alert("WARNING", f"position {market} ({venue}) unsettled "
                                       f"{(ts - close) / 3600.0:.0f}h past close")
            if venue == "pmus" and ts >= close + release_s and market not in self.unresolved:
                cost = float(pos["yes_cost"]) + float(pos["no_cost"])
                self.unresolved[market] = {"venue": venue, "since": float(ts), "close_ts": close,
                                           "cost_usd": round(cost, 6)}
                self._sync_inventory(market)
                self._state_dirty = True
                self._alert("WARNING", f"position {market} unresolved {release_s / 3600.0:.0f}h past "
                                       f"close: ${cost:.2f} released from budgets, marked as a full loss")
        self.settle_view = tuple(view)
        if ts - self._fv_settle_at >= 60.0:
            self._fv_settle_refresh(ts)

    def _fv_calib_close(self, market: str, pend: dict):
        """Close of a market with pending calibration samples: the watched
        close, else its program's close, else the latest sample's
        settlement-window end (sample time + lead)."""
        w = self.fv_calib_watch.get(market)
        if w is not None and w.get("close_ts") is not None:
            return float(w["close_ts"])
        prog = self.programs.get(market)
        if prog is not None and prog.close_ts is not None:
            return float(prog.close_ts)
        ends = [float(x["ts"]) + float(x["lead_h"]) * 3600.0 for x in (pend.get("samples") or {}).values()]
        return max(ends) if ends else None

    def _fv_settle_refresh(self, ts: float) -> None:
        """Refresh ``fv_settle_view``: Kalshi markets whose model fair values
        await scoring (``fv_calib.pending``) and closed more than
        LIP_FV_CALIB_BACKFILL_AFTER_S (12 h) ago, oldest close first. The
        websocket lifecycle channel normally settles them before that; these
        are the ones it missed (e.g. while disconnected). The delay keeps the
        read-only REST backfill from polling every closed market until its
        result is out."""
        self._fv_settle_at = float(ts)
        delay = _env_num("LIP_FV_CALIB_BACKFILL_AFTER_S", 12 * 3600.0)
        due = []
        for market, pend in self.fv_calib.pending.items():
            if market.startswith("PMUS:") or market in self.settled:
                continue
            try:
                close = self._fv_calib_close(market, pend)
            except (TypeError, ValueError, KeyError):
                continue
            if close is not None and ts >= close + delay:
                due.append((close, market))
        self.fv_settle_view = tuple(m for _c, m in sorted(due))

    def unresolved_report(self) -> dict:
        """Status ``unresolved_positions``: released PM US positions."""
        return {m: dict(v) for m, v in self.unresolved.items()}

    def _archive_period(self, market: str, acc: SecondAccrual) -> None:
        try:
            raw = acc.raw_usd()
            if acc.max_reward_usd is not None:
                raw = min(raw, acc.max_reward_usd)
        except Exception:
            logging.getLogger("lip.risk").exception("archiving accrual for %s failed", market)
            return
        if float(raw) <= 0.0:
            return  # nothing earned (never quoted): nothing to keep
        self.closed_periods[market] = self.closed_periods.get(market, 0.0) + float(raw)
        self.closed_periods_n += 1
        self._state_dirty = True

    # ------------------------------------------------------------ marks / P&L
    def _note_mark(self, market: str) -> None:
        """Remember the market's YES mark (cents) from a usable book: the mid
        when both sides quote, else the remaining side (YES bid, or 100 - NO
        bid). An empty or stale book keeps the previous mark."""
        acc = self.accruals.get(market)
        if acc is None or market in self.settled:
            return
        book = acc.book.book
        if not book.is_usable():
            return
        yb = max((lvl.price_cents for lvl in book.yes_bids), default=None)
        nb = max((lvl.price_cents for lvl in book.no_bids), default=None)
        if yb is not None and nb is not None:
            self.last_mid[market] = (yb + (100 - nb)) / 2.0
        elif yb is not None:
            self.last_mid[market] = float(yb)
        elif nb is not None:
            self.last_mid[market] = float(100 - nb)

    def _yes_mark(self, market: str):
        """(YES mark cents or None, source): settlement value, else the
        live/last-known mark."""
        if market in self.settled:
            return (100.0 if self.settled[market]["result"] == "yes" else 0.0), "settled"
        self._note_mark(market)
        mark = self.last_mid.get(market)
        return mark, ("mark" if mark is not None else "none")

    def _fill_fee_usd(self, market: str, price_cents: float, count: float) -> float:
        """Kalshi maker fee for one paper fill (mm.accounting.kalshi_fee_usd)
        with the program's fee_type and multiplier; a missing or unknown
        fee_type is charged as the standard maker fee. PM US makers pay no fee
        (their rebate is booked separately)."""
        if self._venue(market) != "kalshi" or count <= 0:
            return 0.0
        from mm.accounting import kalshi_fee_usd
        prog = self.programs.get(market)
        fee_type = (prog.fee_type if prog is not None else "") or "quadratic_with_maker_fees"
        mult = Decimal(str(prog.fee_multiplier if prog is not None else 1.0))
        try:
            fee = kalshi_fee_usd(int(round(price_cents)), count, fee_type=fee_type, multiplier=mult)
        except ValueError:
            fee = kalshi_fee_usd(int(round(price_cents)), count,
                                 fee_type="quadratic_with_maker_fees", multiplier=mult)
        return float(fee)

    def pnl_parts(self) -> dict:
        """Session P&L parts from held positions (USD).

        markout_usd = MTM value of every position at its mark (book mid,
        remaining side of a one-sided book, last known mark, or settlement)
        minus its cost. A position that never had a mark is valued at 0
        (worst case) and listed in ``unmarked``; a released unresolved
        position is valued at 0 too. Plus the realized markout of settled
        positions already dropped (``realized_pruned_usd``)."""
        markout = 0.0
        unmarked, unsettled = [], []
        for market, pos in self.position.items():
            mark, source = self._yes_mark(market)
            cost = float(pos["yes_cost"]) + float(pos["no_cost"])
            if market in self.unresolved:
                markout -= cost  # released unresolved: a full loss until settled
                continue
            if mark is None:
                value = 0.0
                unmarked.append(market)
            else:
                value = (float(pos["yes"]) * mark + float(pos["no"]) * (100.0 - mark)) / 100.0
            markout += value - cost
            prog = self.programs.get(market)
            close = None if prog is None else prog.close_ts
            if source != "settled" and (prog is None or (close is not None and self.now and self.now >= close)):
                unsettled.append(market)
        markout += self.realized_pruned_usd
        return {"markout_usd": markout, "fees_usd": self.fees_usd_total,
                "rebates_usd": self.pm_rebate_usd, "unmarked": unmarked, "unsettled": unsettled}

    def session_mtm_usd(self) -> float:
        parts = self.pnl_parts()
        return parts["markout_usd"] - parts["fees_usd"] + parts["rebates_usd"]

    def daily_pnl_usd(self) -> Decimal:
        """Today's (UTC, loop clock) MTM P&L: MTM of every held position
        (restored from the state file across restarts) minus fees plus PM US
        rebates, minus its value when the day started. Rewards excluded.
        The day base is persisted (``pnl_day``), so a restart on the same day
        keeps counting from the same base and a restart on a later day starts
        that day at 0. With no base (first start: nothing held, base 0; a
        restored state file without one: base = the restored MTM, which is
        not today's). Cached per loop second."""
        key = (int(self.now or 0), self.fills_total, len(self.settled))
        cached = getattr(self, "_daily_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        cur = self.session_mtm_usd()
        day = datetime.fromtimestamp(self.now or time.time(), timezone.utc).date().isoformat()
        if self._pnl_day is None:
            base = cur if getattr(self, "_pnl_day_unknown", False) else 0.0
            self._pnl_day = {"day": day, "base": base}
            self._pnl_day_unknown = False
            self._state_dirty = True
        elif self._pnl_day.get("day") != day:
            self._pnl_day = {"day": day, "base": cur}
            self._state_dirty = True
        out = Decimal(str(round(cur - float(self._pnl_day["base"]), 6)))
        self._daily_cache = (key, out)
        return out

    # ------------------------------------------------------------ inventory risk
    def _inv_exposure_usd(self, market: str) -> Decimal:
        """Capital held by a market's filled position. Contracts net YES vs
        NO: the paired part (min(yes, no)) locks the cost of both legs until
        it settles at $1 a pair, and the unpaired part can lose all of its
        cost. Paired cost + unpaired cost = the cost basis of both legs.
        0 once settled or released as unresolved."""
        pos = self.position.get(market)
        if not pos or market in self.settled or market in self.unresolved:
            return Decimal(0)
        return Decimal(str(round(float(pos["yes_cost"]) + float(pos["no_cost"]), 6)))

    def _sync_inventory(self, market: str) -> None:
        """Keep the risk engine's market/venue dollars = resting commitment +
        held inventory (``inv_committed``)."""
        new = self._inv_exposure_usd(market)
        old = self.inv_committed.get(market, Decimal(0))
        delta = new - old
        if delta == 0:
            return
        venue = (self.position.get(market) or {}).get("venue") or self._venue(market)
        self.risk.market_usd[market] = Decimal(str(self.risk.market_usd.get(market, 0))) + delta
        self.risk.venue_usd[venue] = Decimal(str(self.risk.venue_usd.get(venue, 0))) + delta
        self.inv_committed[market] = new

    def locked_usd(self) -> dict:
        """Inventory capital per venue (see ``_inv_exposure_usd``)."""
        out: dict[str, float] = {}
        for market, usd in self.inv_committed.items():
            venue = (self.position.get(market) or {}).get("venue") or self._venue(market)
            out[venue] = out.get(venue, 0.0) + float(usd)
        return out

    def _side_room_contracts(self, market: str, side: str, price_cents: int):
        """Contracts of ``side`` we may still rest without breaching the
        unpaired-inventory caps (LIP_MARKET_INV_CAP_USD, LIP_EVENT_INV_CAP_USD)
        if all of it filled. Contracts that pair existing inventory are always
        allowed. None when no cap is set."""
        cap_m = _env_num("LIP_MARKET_INV_CAP_USD", 0.0)
        cap_e = _env_num("LIP_EVENT_INV_CAP_USD", 0.0)
        if cap_m <= 0 and cap_e <= 0:
            return None
        price = max(1, int(price_cents))
        u = self._unpaired(market, side)
        pair = max(0.0, -u)
        here = self._unpaired_usd(market)
        rooms = []
        if cap_m > 0:
            rooms.append(cap_m - (here if u > 0 else 0.0))
        if cap_e > 0:
            ev = self._event_of(market)
            held = sum(self._unpaired_usd(m) for m in self.position if self._event_of(m) == ev)
            if u < 0:
                held -= here  # the pairing contracts remove this market's unpaired dollars
            rooms.append(cap_e - held)
        room = max(0.0, min(rooms))
        return pair + room * 100.0 / price

    def _recheck_event_caps(self, market: str, ts: float) -> None:
        """After a fill: re-clamp every resting quote in the same event to the
        remaining inventory room now, not at the next selection."""
        if _env_num("LIP_MARKET_INV_CAP_USD", 0.0) <= 0 and _env_num("LIP_EVENT_INV_CAP_USD", 0.0) <= 0:
            return
        ev = self._event_of(market)
        for m in list(self.resting):
            if self._event_of(m) != ev:
                continue
            quote = self.resting.get(m)
            if quote is None:
                continue
            over = False
            for sd in ("yes", "no"):
                size = float(quote.get(sd) or 0)
                if size <= 0:
                    continue
                room = self._side_room_contracts(m, sd, int(quote[f"{sd}_cents"]))
                if room is not None and size > int(room) + 1e-9:
                    over = True
            if not over:
                continue
            self.cap_trims_n += 1
            sides = tuple(sd for sd in ("yes", "no") if float(quote.get(sd) or 0) > 0)
            size = max(float(quote.get("yes") or 0), float(quote.get("no") or 0))
            best0 = quote.get("best0")
            if self._quote(m, int(quote["yes_cents"]), int(quote["no_cents"]), size, ts,
                           sides=sides, skewed=True) and best0 is not None and m in self.resting:
                self.resting[m]["best0"] = best0

    # ------------------------------------------------------------ kill / alerts
    def _latch_kill(self, reason: str, *, cancel_all: bool = True) -> None:
        """Latch the engine kill (quoting stops), alert once, cancel all."""
        first = self.kill is None
        self.kill = {"reason": reason, "cancel_all": cancel_all,
                     "paper": self.mode == "paper", "ts": self.now or None}
        if first:
            self._alert("CRITICAL", f"engine kill latched: {reason}")
        self._state_dirty = True
        if cancel_all:
            self._cancel_all(reason)

    def _alert(self, level: str, message: str) -> None:
        """Existing alert path (monitor.alerts: alerts.log + journal) and the
        status ``engine_alerts`` list."""
        self.alerts.append({"ts": self.now or time.time(), "level": level, "message": message})
        del self.alerts[:-50]
        logging.getLogger("lip.risk").critical("ALERT %s: %s", level, message)
        try:
            from monitor.alerts import alert
            alert(level, "lip_unattended", message)
        except Exception:
            logging.getLogger("lip.risk").exception("alert delivery failed")

    # ------------------------------------------------------------ persistence
    def state_dict(self) -> dict:
        """Positions, fill/fee aggregates, rolled periods, cooldowns and an
        internal kill latch. An external (kill-file) kill is not saved: the
        kill file itself is the source of truth for it."""
        kill = self.kill
        if kill is not None and str(kill.get("reason", "")).startswith("external_kill:"):
            kill = kill.get("prev")
        self.daily_pnl_usd()  # the day base is saved even if no status was built yet
        return {
            "version": STATE_VERSION, "saved_ts": time.time(), "now": self.now, "mode": self.mode,
            "position": self.position, "bucket_pos": self.bucket_pos,
            "bucket_of": {m: b for m, b in (getattr(self, "bucket_of", {}) or {}).items() if m in self.position},
            "last_mid": {m: v for m, v in self.last_mid.items() if m in self.position},
            "settled": self.settled, "fills_total": self.fills_total, "fills_by_venue": self.fills_by_venue,
            "fills_synthetic_by_venue": self.fills_synthetic_by_venue,
            "premium_usd_total": self.premium_usd_total, "fees_usd_total": self.fees_usd_total,
            "pm_rebate_usd": self.pm_rebate_usd, "closed_periods": self.closed_periods,
            "closed_periods_n": self.closed_periods_n, "closed_periods_agg": self.closed_periods_agg,
            "realized_pruned_usd": self.realized_pruned_usd, "bucket_closed": self.bucket_closed,
            "settled_lifetime": self.settled_lifetime,
            "cooldown": [[k[0], k[1], v] for k, v in self.cooldown.items()],
            "kill": kill, "pnl_day": self._pnl_day,
            "markouts": self.markouts.state(),
            "unresolved": self.unresolved,
            "fv_calibration": self.fv_calib.state(),
            "fv_calib_watch": self.fv_calib_watch,
        }

    def attach_state(self, path: str) -> None:
        """Load ``path`` when it exists and persist there from now on.

        A file that exists but cannot be read or validated is never ignored
        and never overwritten: the engine latches a kill (no quoting) until the
        operator moves the file aside and restarts."""
        self.state_path = str(path)
        p = Path(path)
        if not p.exists():
            logging.getLogger("lip.risk").info("no engine state at %s: starting flat", path)
            return
        try:
            self._restore_state(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            self.state_error = f"{type(exc).__name__}: {exc}"[:300]
            self._latch_kill(f"state_file_unreadable: {path} ({self.state_error}); "
                             "move it aside and restart to reset", cancel_all=True)
            return
        logging.getLogger("lip.risk").info(
            "engine state restored from %s: %d positions, %d fills", path, len(self.position), self.fills_total)

    def _restore_state(self, data: dict) -> None:
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise ValueError(f"unsupported state version {data.get('version') if isinstance(data, dict) else None!r}")
        position = {}
        for market, pos in dict(data.get("position") or {}).items():
            row = {k: float(pos[k]) for k in ("yes", "no", "yes_cost", "no_cost")}
            row["fees"] = float(pos.get("fees", 0.0))
            row["venue"] = str(pos.get("venue") or "kalshi")
            for key in ("close_ts", "orphan_ts"):
                if pos.get(key) is not None:
                    row[key] = float(pos[key])
            position[str(market)] = row
        bucket_pos = {}
        for bucket, rows in dict(data.get("bucket_pos") or {}).items():
            bucket_pos[str(bucket)] = {str(m): {k: float(v) for k, v in dict(r).items()}
                                       for m, r in dict(rows).items()}
        settled = {str(m): {"result": str(v["result"]), "ts": v.get("ts"),
                            "source": str(v.get("source") or "ws_lifecycle")}
                   for m, v in dict(data.get("settled") or {}).items()}
        unresolved = {str(m): {"venue": str(v.get("venue") or "pmus"), "since": float(v["since"]),
                               "close_ts": None if v.get("close_ts") is None else float(v["close_ts"]),
                               "cost_usd": float(v.get("cost_usd") or 0.0)}
                      for m, v in dict(data.get("unresolved") or {}).items()}
        cooldown = {(str(m), str(sd)): float(until) for m, sd, until in list(data.get("cooldown") or [])}
        raw_life = data.get("settled_lifetime")
        if raw_life is not None:
            if not isinstance(raw_life, dict) or not isinstance(raw_life.get("by_venue", {}), dict):
                raise ValueError("settled_lifetime must be an object with a by_venue object")
            by_venue = {str(k): int(v) for k, v in dict(raw_life.get("by_venue") or {}).items()}
            if any(v < 0 for v in by_venue.values()):
                raise ValueError("settled_lifetime counts must be >= 0")
            settled_lifetime = {"by_venue": by_venue, "total_usd": float(raw_life.get("total_usd") or 0.0),
                                "lower_bound": bool(raw_life.get("lower_bound"))}
        else:
            # Written before the lifetime counters: count the settled rows
            # still held (pruned ones are gone: a lower bound); the USD total
            # is exact (pruned realized P&L was kept in realized_pruned_usd).
            by_venue, total = {}, float(data.get("realized_pruned_usd") or 0.0)
            for m, v in settled.items():
                pos = position.get(m)
                if not pos or (pos["yes"] <= 0.0 and pos["no"] <= 0.0):
                    continue
                by_venue[pos["venue"]] = by_venue.get(pos["venue"], 0) + 1
                total += self._settle_value_usd(pos, v["result"])
            settled_lifetime = {"by_venue": by_venue, "total_usd": total, "lower_bound": True}
        kill = data.get("kill")
        if kill is not None and not isinstance(kill, dict):
            raise ValueError("kill must be an object")
        from mm.unattended.markouts import MarkoutBook
        markouts = MarkoutBook()
        if data.get("markouts") is not None:  # absent in state files written before Phase 4
            markouts.load_state(data["markouts"])
        from mm.unattended.fv_calib import FVCalibration
        fv_calib = FVCalibration()
        if data.get("fv_calibration") is not None:  # absent before model fair value
            fv_calib.load_state(data["fv_calibration"])
        watch = {}
        for m, w in dict(data.get("fv_calib_watch") or {}).items():  # absent before the watch
            watch[str(m)] = {"series": str(w["series"]),
                             "close_ts": None if w.get("close_ts") is None else float(w["close_ts"]),
                             "added_ts": float(w.get("added_ts") or 0.0)}
        # validated: apply
        self.position = position
        self.bucket_pos = bucket_pos
        self.settled = settled
        self.unresolved = {m: v for m, v in unresolved.items() if m in position and m not in settled}
        self.last_mid.update({str(m): float(v) for m, v in dict(data.get("last_mid") or {}).items()})
        if not hasattr(self, "bucket_of"):
            self.bucket_of = {}
        self.bucket_of.update({str(m): str(b) for m, b in dict(data.get("bucket_of") or {}).items()})
        self.fills_total = int(data.get("fills_total") or 0)
        self.fills_by_venue = {str(k): int(v) for k, v in dict(data.get("fills_by_venue") or {}).items()}
        self.fills_synthetic_by_venue = {str(k): int(v) for k, v in
                                         dict(data.get("fills_synthetic_by_venue") or {}).items()}
        self.premium_usd_total = float(data.get("premium_usd_total") or 0.0)
        self.fees_usd_total = float(data.get("fees_usd_total") or 0.0)
        self.pm_rebate_usd = float(data.get("pm_rebate_usd") or 0.0)
        self.closed_periods = {str(m): float(v) for m, v in dict(data.get("closed_periods") or {}).items()}
        self.closed_periods_n = int(data.get("closed_periods_n") or 0)
        self.closed_periods_agg = {str(k): float(v) for k, v in dict(data.get("closed_periods_agg") or {}).items()}
        self.realized_pruned_usd = float(data.get("realized_pruned_usd") or 0.0)
        self.settled_lifetime = settled_lifetime
        self.bucket_closed = {str(b): {str(k): float(v) for k, v in dict(r).items()}
                              for b, r in dict(data.get("bucket_closed") or {}).items()}
        self.cooldown.update(cooldown)
        self._pnl_day = data.get("pnl_day") if isinstance(data.get("pnl_day"), dict) else None
        # No saved day base but restored positions: their MTM is not today's.
        self._pnl_day_unknown = self._pnl_day is None and bool(self.position)
        self._daily_cache = None
        self.markouts = markouts
        self.fv_calib = fv_calib
        self.fv_calib_watch = watch
        self.fv_calib_view = frozenset(watch)
        for market in self.position:
            self._sync_inventory(market)
        if kill is not None:
            self.kill = dict(kill)
            self._alert("CRITICAL", f"engine kill restored from state file: {kill.get('reason')}")

    def state_snapshot(self, *, force: bool = False, every_s: float = 0.0) -> str | None:
        """The state JSON to write, or None (no state file, a state file that
        failed to load, nothing changed, or saved less than ``every_s`` ago).
        Call under ``lock``; the result is a string, so ``write_state`` can
        do the disk I/O after the lock is released. Clears the dirty flag
        (``write_state`` sets it again on failure)."""
        if not self.state_path or self.state_error is not None:
            return None
        if not force and (not self._state_dirty or time.time() - self._state_saved_at < every_s):
            return None
        body = json.dumps(self.state_dict(), default=str)
        self._state_dirty = False
        self._state_saved_at = time.time()
        return body

    def write_state(self, body: str) -> None:
        """Atomic write of a ``state_snapshot`` (tmp + fsync + rename). Needs
        no lock. On failure the state is marked dirty again and the error
        raised."""
        import threading as _threading
        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{_threading.get_ident()}")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            self._state_dirty = True
            self._state_saved_at = 0.0
            raise
        self._state_save_failed = False

    def note_state_save_failed(self, exc: BaseException) -> None:
        """Alert once per failure streak (call under ``lock``)."""
        if not getattr(self, "_state_save_failed", False):
            self._alert("CRITICAL", f"engine state save failed: {type(exc).__name__}: {exc}")
        self._state_save_failed = True

    def save_state(self, *, force: bool = False) -> bool:
        """Snapshot and write now (clean shutdown, tests). Never writes over
        a state file that failed to load."""
        body = self.state_snapshot(force=force)
        if body is None:
            return False
        self.write_state(body)
        return True

    def maybe_save_state(self, every_s: float = 5.0) -> bool:
        """Debounced save: at most once per ``every_s`` while dirty. The
        service's timer uses state_snapshot/write_state instead so the write
        runs outside the lock."""
        body = self.state_snapshot(every_s=every_s)
        if body is None:
            return False
        try:
            self.write_state(body)
            return True
        except Exception as exc:
            self.note_state_save_failed(exc)
            return False

    # ------------------------------------------------------------ patch 21
    def _venue(self, market: str) -> str:
        prog = self.programs.get(market)
        return prog.venue if prog is not None else "kalshi"

    def _venue_of(self, market: str) -> str:
        """Venue of a market that may no longer have a program (pruned):
        its program, else its held position, else the PM US ticker prefix."""
        prog = self.programs.get(market)
        if prog is not None:
            return prog.venue
        pos = self.position.get(market)
        if pos and pos.get("venue"):
            return str(pos["venue"])
        return "pmus" if str(market).startswith("PMUS:") else "kalshi"

    def _model_row_allowed(self, market: str, row: dict, *, guard: bool = False) -> bool:
        """A model (fv_weather) fair value is used only in paper mode, and by
        the defensive guard only when FV quoting is on for its family; other
        sources (Polymarket match, rain ensemble) are not restricted here."""
        from mm.unattended.fv_weather import SOURCE
        if row.get("source") != SOURCE:
            return True
        if self.mode != "paper":
            return False
        if guard:
            from mm.unattended.fairvalue import fv_quote_active
            prog = self.programs.get(market)
            series = prog.series if prog is not None else str(market).split("-", 1)[0]
            return fv_quote_active(series)
        return True

    def _fv_side_cents(self, market: str, side: str):
        """External fair value of one side in cents, or None when the fair
        value cache (Kalshi markets only) has no current value for it (a
        model value outside paper mode counts as none)."""
        if self.fv is None or self._venue(market) != "kalshi":
            return None
        row = self.fv.get(market, now=time.time())
        if row is None or row.get("fv_cents") is None or not self._model_row_allowed(market, row):
            return None
        fv = float(row["fv_cents"])
        return fv if side == "yes" else 100.0 - fv

    def _fv_note_program(self, market: str, prog: "_Program", row: dict) -> None:
        """Model families (FV quoting or calibration on): ask the fair-value
        cache to price this market (its targets include ``_fv_wanted``), put
        it in the calibration watch and hand the cache the strike fields from
        the screen's metadata."""
        from mm.unattended.fairvalue import fv_model_active, fv_model_supports
        if prog.venue != "kalshi" or not fv_model_active(prog.series) or not fv_model_supports(prog.series):
            return
        self._fv_wanted.add(market)
        self._fv_calib_watch_add(market, prog.series, prog.close_ts)
        hint = {k: row.get(k) for k in ("strike_type", "floor_strike", "cap_strike") if row.get(k) is not None}
        if hint:
            self._fv_hints[market] = hint
            self._fv_push_hint(market)

    def _fv_push_hint(self, market: str) -> None:
        note = getattr(self.fv, "note_market", None)
        hint = self._fv_hints.get(market)
        if note is not None and hint and market not in (getattr(self.fv, "hints", None) or {}):
            note(market, hint)

    def _fv_quote_row(self, market: str):
        """The usable model fair-value row that drives quoting for
        ``market``, or None (fail closed: the defensive behaviour applies).
        Usable = paper mode, LIP_FV_QUOTE_ENABLE, a Kalshi market of a
        LIP_FV_QUOTE_FAMILIES series, a current row from the model source
        (fv_weather) with confidence >= LIP_FV_MIN_CONF."""
        from mm.unattended import fairvalue as fvm
        from mm.unattended.fv_weather import SOURCE
        if self.fv is None or self.mode != "paper" or not fvm.fv_quote_enabled():
            return None
        prog = self.programs.get(market)
        if prog is None or prog.venue != "kalshi" or not fvm.fv_quote_active(prog.series):
            return None
        row = self.fv.get(market, now=time.time())
        if row is None or row.get("source") != SOURCE or row.get("fv_cents") is None:
            return None
        try:
            fv, conf = float(row["fv_cents"]), float(row.get("conf") or 0.0)
        except (TypeError, ValueError):
            return None
        if not (0.0 < fv < 100.0) or conf < fvm.fv_min_conf():
            return None
        return row

    def _fv_count(self, key: str, side: str | None = None) -> None:
        k = key if side is None else f"{key}_{side}"
        self.fv_quote_stats[k] = self.fv_quote_stats.get(k, 0) + 1

    def _fv_gate_sides(self, market: str, row: dict, yes_cents: int, no_cents: int,
                       sides: tuple, size: float | None) -> tuple:
        """Sides allowed to rest under FV-driven quoting: edge >= -
        LIP_FV_MAX_GIVEUP_CENTS and, when ``size`` is given, per-side EV > 0
        (edge x fills/day + that side's LIP reward - maker fees). The reward
        of a side resting with its opposite is half the two-sided reward; a
        lone side earns its one-sided snapshot share (Kalshi excludes a
        snapshot whose other side does not reach target without us)."""
        from mm.unattended.fairvalue import fv_side_ok, side_edge_cents, side_ev_usd_day
        fv = float(row["fv_cents"])
        keep = []
        for sd in sides:
            price = yes_cents if sd == "yes" else no_cents
            edge = side_edge_cents(fv, sd, price)
            if not fv_side_ok(edge):
                self._fv_count("edge_withheld", sd)
                continue
            keep.append(sd)
        if size is None or not keep:
            return tuple(keep)
        from mm.selector import (fills_per_day, kalshi_one_sided_share, kalshi_share, maker_fee_usd,
                                 reward_per_day)
        km = self._km(market)
        if km is None:
            return ()
        try:
            if len(keep) == 2:
                two = reward_per_day(kalshi_share(km, int(yes_cents), int(no_cents), float(size)), km) / 2.0
                reward = {"yes": two, "no": two}
            else:
                sd = keep[0]
                price = int(yes_cents if sd == "yes" else no_cents)
                reward = {sd: reward_per_day(kalshi_one_sided_share(km, sd, price, float(size)), km)}
        except Exception:
            logging.getLogger("lip.risk").exception("fv reward estimate failed for %s", market)
            return ()
        fills = fills_per_day(km, float(size))
        out = []
        for sd in keep:
            price = int(yes_cents if sd == "yes" else no_cents)
            fee = maker_fee_usd(km, price, min(float(size), fills) if fills > 0 else 0.0)
            ev = side_ev_usd_day(side_edge_cents(fv, sd, price), fills, reward[sd], fee)
            if ev > 0:
                out.append(sd)
            else:
                self._fv_count("ev_withheld", sd)
        return tuple(out)

    FV_CALIB_WATCH_MAX = 2000
    FV_CALIB_WATCH_MAX_AGE_S = 10 * 86400.0

    def _fv_calib_watch_add(self, market: str, series: str, close_ts) -> None:
        w = self.fv_calib_watch.get(market)
        if w is not None:
            if close_ts is not None and w.get("close_ts") != float(close_ts):
                w["close_ts"] = float(close_ts)
                self._state_dirty = True
            return
        if len(self.fv_calib_watch) >= self.FV_CALIB_WATCH_MAX:
            oldest = min(self.fv_calib_watch, key=lambda m: self.fv_calib_watch[m]["added_ts"])
            del self.fv_calib_watch[oldest]
        self.fv_calib_watch[market] = {"series": str(series),
                                       "close_ts": None if close_ts is None else float(close_ts),
                                       "added_ts": float(self.now or time.time())}
        self.fv_calib_view = frozenset(self.fv_calib_watch)
        self._state_dirty = True

    def _fv_calib_watch_expire(self, ts: float) -> None:
        """Forget watched markets at their close (or 10 days after they were
        added, the fv_calib pending horizon, when no close is known)."""
        old = [m for m, w in self.fv_calib_watch.items()
               if (w.get("close_ts") is not None and ts >= float(w["close_ts"]))
               or ts - float(w["added_ts"]) > self.FV_CALIB_WATCH_MAX_AGE_S]
        for m in old:
            del self.fv_calib_watch[m]
        if old:
            self.fv_calib_view = frozenset(self.fv_calib_watch)
            self._state_dirty = True

    def fv_cache_targets(self) -> set:
        """Markets the fair-value cache prices (called from its thread):
        resting markets, the loop's FV targets and the calibration watch.
        Kalshi only."""
        wanted = set(self.resting.copy()) | self._fv_wanted.copy() | set(self.fv_calib_view)
        return {m for m in wanted if not m.startswith("PMUS:")}

    def _fv_calib_tick(self, ts: float) -> None:
        """Every LIP_FV_CALIB_EVERY_S (30 s): record each new model fair value
        of a model-priced FV-family market with the book mid now
        (``fv_calib``): every fed market of the family (quoted or not) and
        every market in the calibration watch until its close, also after
        its program left the loop (then without a book: mid None, a
        model-only sample). Values below LIP_FV_MIN_CONF are recorded too
        (their conf is kept; fv_calib keeps them out of the headline). The
        markets of one city-day event priced at this tick are also recorded
        as one bucket distribution (``fv_calib.record_event``). Quoting
        reads only the verdict (``fv_calib.passed``, selection credit)."""
        if self.fv is None or ts - self._fv_calib_at < _env_num("LIP_FV_CALIB_EVERY_S", 30.0):
            return
        self._fv_calib_at = ts
        from mm.unattended.fairvalue import fv_model_active
        from mm.unattended.fv_calib import event_of
        from mm.unattended.fv_weather import SOURCE
        self._fv_calib_watch_expire(ts)
        by_event: dict = {}
        for market in sorted(self._fv_wanted | set(self.fv_calib_watch)):
            prog = self.programs.get(market)
            if prog is not None:
                if prog.venue != "kalshi":
                    continue
                series = prog.series
            elif market in self.fv_calib_watch:
                series = self.fv_calib_watch[market]["series"]
            else:
                continue
            if not fv_model_active(series):
                continue
            row = self.fv.get(market, now=time.time())
            if row is None or row.get("source") != SOURCE or row.get("fv_cents") is None:
                continue
            mid = None
            acc = self.accruals.get(market)
            if acc is not None and acc.book.book.is_usable():
                yb, nb = self._best(market)
                if yb is not None and nb is not None:
                    mid = (yb + (100 - nb)) / 2.0
            window = row.get("window")
            lead_h = (max(0.0, (float(window[1]) - time.time()) / 3600.0)
                      if isinstance(window, (list, tuple)) and len(window) == 2
                      else float(row.get("lead_h") or 0.0))
            rng = row.get("range")
            try:
                if self.fv_calib.record(market, series.upper(), ts, float(row["fv_cents"]),
                                        float(row.get("conf") or 0.0), lead_h, mid, float(row.get("ts") or 0.0),
                                        rng=rng if isinstance(rng, (list, tuple)) else None,
                                        ens=row.get("ens") if isinstance(row.get("ens"), dict) else None):
                    self._state_dirty = True
            except (TypeError, ValueError):
                continue
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                ev = by_event.setdefault(event_of(market), {"station": series.upper(), "lead_h": lead_h,
                                                            "entries": []})
                ev["entries"].append({"market": market, "range": list(rng), "fv": float(row["fv_cents"]),
                                      "conf": float(row.get("conf") or 0.0), "mid": mid})
        for event, ev in by_event.items():
            if len(ev["entries"]) >= 2 and self.fv_calib.record_event(event, ev["station"], ts, ev["lead_h"],
                                                                      ev["entries"]):
                self._state_dirty = True

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
            self._record_fill(fill, ts)

    def _record_fill(self, fill: dict, ts: float) -> bool:
        """One paper fill: bounded history + exact aggregates, inventory,
        fees, the fills-per-minute clock. False when the fill latched a kill."""
        self.fills.append(fill)
        self.fills_total += 1
        venue = self._venue(str(fill.get("market_ticker")))
        self.fills_by_venue[venue] = self.fills_by_venue.get(venue, 0) + 1
        if fill.get("synthetic"):
            self.fills_synthetic_by_venue[venue] = self.fills_synthetic_by_venue.get(venue, 0) + 1
        self.premium_usd_total += float(fill.get("count") or 0) * float(fill.get("price_cents") or 0) / 100.0
        if len(self.fills) > LIST_CAP:
            del self.fills[: len(self.fills) - LIST_CAP // 2]
        self._reduce_resting(fill)
        self._note_fill(fill, ts)
        decision = self.risk.record_fill(1, now=ts)
        if not decision.allowed:
            self._latch_kill(decision.reason, cancel_all=decision.cancel_all)
            return False
        return True

    # ------------------------------------------------------------ patch 15
    def _note_fill(self, fill: dict, ts: float) -> None:
        """Inventory, markout marks, log line; optional side cooldown + pull."""
        market = str(fill["market_ticker"])
        side = str(fill.get("side"))
        count = float(fill.get("count") or 0)
        price = float(fill.get("price_cents") or 0)
        venue = self._venue(market)
        pos = self.position.setdefault(market, {"yes": 0.0, "no": 0.0, "yes_cost": 0.0, "no_cost": 0.0,
                                                "fees": 0.0, "venue": venue})
        pos.setdefault("fees", 0.0)
        pos.setdefault("venue", venue)
        prog = self.programs.get(market)
        if prog is not None and prog.close_ts is not None:
            pos["close_ts"] = float(prog.close_ts)
        bucket = (getattr(self, "bucket_of", {}) or {}).get(market) or "short"
        bpos = self.bucket_pos.setdefault(bucket, {}).setdefault(
            market, {"yes": 0.0, "no": 0.0, "yes_cost": 0.0, "no_cost": 0.0, "fees": 0.0, "fills_n": 0.0})
        fee = self._fill_fee_usd(market, price, count) if side in ("yes", "no") else 0.0
        if side in ("yes", "no"):
            pos[side] += count
            pos[f"{side}_cost"] += count * price / 100.0
            pos["fees"] += fee
            bpos[side] += count
            bpos[f"{side}_cost"] += count * price / 100.0
            bpos["fees"] += fee
            self.fees_usd_total += fee
        bpos["fills_n"] += 1
        if fill.get("synthetic"):
            bpos["synthetic_n"] = bpos.get("synthetic_n", 0.0) + 1
        self._sync_inventory(market)
        self._state_dirty = True
        mid = self._side_mid_cents(market, side)
        if self._venue(market) == "pmus" and count > 0:
            # PM US maker rebate 0.0125 x C x p x (1-p), per fill, banker's
            # rounded to the cent (https://docs.polymarket.us/fees).
            from mm.accounting import pm_us_maker_rebate_usd
            rebate = float(pm_us_maker_rebate_usd(int(round(price)), count))
            self.pm_rebate_usd += rebate
            bpos["rebates"] = bpos.get("rebates", 0.0) + rebate
        self.markouts.add(market=market, side=side, price_cents=price, count=count, ts=ts,
                          venue=venue, bucket=bucket, mid0=mid, synthetic=bool(fill.get("synthetic")))
        self.fill_marks.append({
            "market": market, "side": side, "price_cents": price, "count": count, "ts": ts,
            "mid0": mid, "venue": self._venue(market), "bucket": (getattr(self, "bucket_of", {}) or {}).get(market),
            "markout_60s": None, "markout_300s": None, "markout_1800s": None,
            "synthetic": bool(fill.get("synthetic")),
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
        else:
            cool = _env_num("LIP_FILL_COOLDOWN_S", 0.0)
            if cool > 0 and side in ("yes", "no"):
                self.cooldown[(market, side)] = max(self.cooldown.get((market, side), 0.0), ts + cool)
                # Stop buying more of what was just hit. The opposite side stays:
                # if it fills it pairs the inventory into a $1 settlement.
                self._drop_side(market, side, "fill_cooldown")
        # Inventory caps act on resting size now, for every market in the event.
        # The state file is saved by the service timer (debounced, <= 1/5 s).
        self._recheck_event_caps(market, ts)

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
        if not pos or market in self.settled:
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

    def _calendar_block(self, market: str, ts: float) -> str:
        """Scheduled-event window (config/event_calendar.py): "scheduled_event",
        "scheduled_event_calendar_error" (configured file unusable: fail
        closed, alerted once per error), or ""."""
        prog = self.programs.get(market)
        why = self.calendar.check(prog.series if prog is not None else "", market, ts)
        if why and why != "scheduled_event" and self._calendar_alerted != self.calendar.error:
            self._calendar_alerted = self.calendar.error
            self._alert("CRITICAL", f"event calendar {self.calendar.path} unusable "
                                    f"({self.calendar.error}): quoting blocked on every market")
        return why

    def _policy_block(self, market: str, ts: float) -> str:
        why = self._calendar_block(market, ts)
        if why:
            return why
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
        if row is None or not self._model_row_allowed(market, row, guard=True):
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
        """Once a second: scheduled-event calendar, event window,
        trade-through, fast move, re-peg."""
        move = _env_num("LIP_PULL_MOVE_CENTS", 0.0)
        cool = _env_num("LIP_MOVE_COOLDOWN_S", 600.0)
        repeg = _env_num("LIP_REPEG_MIN_S", 0.0)
        for market in list(self.resting):
            quote = self.resting.get(market)
            if quote is None or market not in self.accruals:
                continue
            why = self._calendar_block(market, ts)
            if why:
                self._pull_one(market, why, ts, 0.0)
                continue
            if self._in_event_window(market, ts):
                self._pull_one(market, "event_window", ts, 0.0)
                continue
            if not self.accruals[market].book.book.is_usable():
                # Fail closed: no guard can run on a stale/empty/off-grid book.
                self._pull_one(market, "book_unusable", ts, 0.0)
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
            if self._fv_fail_closed(market):
                self._fv_count("unavailable")
                self._pull_one(market, "fv_unavailable", ts, 0.0)
                continue
            fv_row = self._fv_quote_row(market)
            if fv_row is not None:
                # FV-driven quoting: a resting side whose edge vs the current
                # fair value is below -LIP_FV_MAX_GIVEUP_CENTS comes out.
                live = tuple(sd for sd in ("yes", "no") if on[sd])
                ok = self._fv_gate_sides(market, fv_row, int(quote["yes_cents"]), int(quote["no_cents"]),
                                         live, None)
                if ok != live:
                    if ok:
                        self.pulls["fv_negative_edge"] = self.pulls.get("fv_negative_edge", 0) + 1
                        size = max(float(quote.get("yes") or 0), float(quote.get("no") or 0))
                        self._quote(market, int(quote["yes_cents"]), int(quote["no_cents"]), size, ts,
                                    sides=ok, skewed=True)
                    else:
                        self._pull_one(market, "fv_negative_edge", ts, 0.0)
                    continue
            fv_drop = () if fv_row is not None else self._fv_drop(market, ts)
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
        caller pulls as before. Orders still in flight (latency) do not fill.
        On a PM US (polled) book the cross is inferred from two REST
        snapshots, not seen trading, so the fill is tagged ``synthetic``
        (low fidelity) like the poller's synthetic prints."""
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
            if self._venue(market) == "pmus":
                fill["synthetic"] = True
            if not self._record_fill(fill, ts):
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
        from mm.selector import quote_economics
        from mm.session_gates import max_contracts_for_fill
        lim = self.risk.limits
        per_market = float(lim.per_market_usd) * alloc_cap_fraction()
        if km.venue == "pmus":
            per_market = min(per_market, pmus_market_cap_usd())
        out = []
        for size in ladder:
            # sides_on prices one-sided quotes (quote_economics ``sides``).
            net, capital, share2, yc, nc = quote_economics(km, float(size), sides=sides_on)
            if yc > 0 and nc > 0:
                legal = min(max_contracts_for_fill(yc, self.fill_cap), max_contracts_for_fill(nc, self.fill_cap))
                if 0 < legal < size:
                    # The last rung is the size the order clamp would rest
                    # (single-fill cap), valued at that size.
                    size = float(legal)
                    net, capital, share2, yc, nc = quote_economics(km, size, sides=sides_on)
                    if out and size <= out[-1][0]:
                        break
            if yc <= 0 or nc <= 0:
                break
            if size > max_contracts_for_fill(yc, self.fill_cap) or size > max_contracts_for_fill(nc, self.fill_cap):
                break
            if capital > per_market + 1e-9:
                break
            value = net - penalty_100 * float(size) / RANK_PENALTY_UNIT * (len(sides_on) / 2.0)
            out.append((float(size), value, capital, int(yc), int(nc)))
            if float(size) not in [float(x) for x in ladder]:
                break
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
            if (book.yes_bids or book.no_bids) and book.is_usable():
                have += 1
        return have >= self.books_ready_fraction * len(self.programs)

    def _maybe_select(self, ts: float) -> None:
        if not self.programs or self._skew_active:
            return
        if self._reselect_pending and self.connected and self._books_ready():
            # Quotes were pulled for a disconnect: re-select as soon as books
            # are usable again instead of waiting for the next period.
            self._reselect_pending = False
            self._select(ts)
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
        return [km for km in (self._km(market) for market in self.programs) if km is not None]

    def _km(self, market: str) -> KalshiMarket | None:
        """Selector view of one program: live book, close, fees, and the
        usable model fair value when FV-driven quoting applies to it."""
        prog = self.programs.get(market)
        if prog is None or market not in self.accruals:
            return None
        book = self.accruals[market].book.book
        row = self._fv_quote_row(market)
        return KalshiMarket(
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
            fv_cents=None if row is None else float(row["fv_cents"]),
            fv_calibrated=row is not None and self.fv_calib.passed(),
        )

    def _fv_note_admitted(self, markets: list) -> None:
        """Markets that pass the selector only because a usable fair value
        applied LIP_FV_MIN_HOURS_TO_CLOSE. Without that value they must not
        rest (``_fv_fail_closed``)."""
        from dataclasses import replace
        from mm.selector import exclusion_reason
        admitted = set()
        for km in markets:
            if km.fv_cents is None:
                continue
            if exclusion_reason(replace(km, fv_cents=None, fv_candidate=False)):
                admitted.add(km.market)
        self._fv_admitted = admitted
        for market in self._fv_hints:
            self._fv_push_hint(market)

    def _fv_fail_closed(self, market: str) -> bool:
        """True when ``market`` was admitted on a fair value that is no
        longer usable: it must not rest (cancel/pull ``fv_unavailable``)."""
        return market in self._fv_admitted and self._fv_quote_row(market) is None

    def _select(self, ts: float) -> None:
        self._select_t0 = time.time()
        self.prune_ended(ts)
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
                                      single_fill_cap_usd=self.screen_fill_cap)
        else:
            selection = allocate(
                markets, bankroll=pool, chunk=self.chunk, max_size=self.chunk,
                per_market_usd=per_market, per_series_usd=per_series,
                per_category_usd=pool, live=live, series_stats=self.series_stats,
                single_fill_cap_usd=self.screen_fill_cap,
            )
        self.excluded = list(selection.excluded)
        self._fv_note_admitted(markets)
        # per_event_usd=pool: optimize_sizes groups by series and would cut a
        # series by RAW objective before the markout-penalised rank pass. The
        # per-series cap is enforced below (alloc_series) in rank order.
        sized = optimize_sizes(
            markets, bankroll=pool, per_market_usd=per_market,
            per_event_usd=pool, total_usd=pool,
            sizes=(self.chunk,), markout_usd_per_contract=0.0,
            single_fill_cap_usd=self.screen_fill_cap,
        )
        chosen = {row.market: row for row in sized.chosen}
        taken = {row.market for row in selection.taken}
        plan = {}
        for row in selection.taken:
            plan[row.market] = {"net_per_day": float(row.net_per_day),
                                "capital_usd": float(row.capital_usd),
                                "net_size": float(row.size)}
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
            # rank_penalty_per_day is $/day per RANK_PENALTY_UNIT (100)
            # contracts per side, both sides; net_per_day was evaluated at
            # net_size contracts per side, so scale it the way _size_curve does.
            penalty = (self.programs[market].rank_penalty_per_day
                       * float(info.get("net_size") or self.chunk) / RANK_PENALTY_UNIT)
            per_dollar = rank_live_score(info.get("net_per_day") or 0.0, penalty, cap)
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
        fv_rows = {}
        for _per, market, _row in wanted:
            fv_row = self._fv_quote_row(market)
            if fv_row is not None:
                fv_rows[market] = fv_row
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
            if not why and market in fv_rows:
                # FV-driven quoting: rest only sides that do not pay up
                # versus fair value at the reference rungs.
                row_o = item[2]
                sides = self._fv_gate_sides(market, fv_rows[market], int(row_o.yes_cents),
                                            int(row_o.no_cents), sides_of[market], None)
                if sides:
                    sides_of[market] = sides
                else:
                    why = "fv_negative_edge"
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
                size = float(min(row.size, max_contracts_for_fill(int(row.yes_cents), self.fill_cap),
                                 max_contracts_for_fill(int(row.no_cents), self.fill_cap)))
                add = (int(row.yes_cents) + int(row.no_cents)) / 100.0 * size
                if size <= 0 or (km.venue == "pmus" and add > pmus_market_cap_usd() + 1e-9):
                    continue
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

    def venue_budgets(self, *, locked: dict | None = None, has_pmus: bool | None = None) -> dict:
        """Patch 21: unified cross-venue allocation budgets for resting quotes.

        Filled inventory (``locked_usd``: cost basis of held positions) is
        capital in use, so it comes off first:
        kalshi = min(fraction x per-venue - kalshi inventory,
                     fraction x gross - all inventory).
        pmus   = min(LIP_PMUS_BUDGET_USD (300) - pmus inventory,
                     fraction x gross - all inventory - kalshi,
                     fraction x per-venue - pmus inventory)
                 when any PM US program is loaded, else 0.
        The sum is then scaled down to LIP_WD_MAX_CAPITAL_USD when that is set
        (the watchdog trips above it). The RiskEngine also checks per-venue and
        gross caps (resting + inventory) on every quote."""
        lim = self.risk.limits
        frac = alloc_cap_fraction()
        locked = self.locked_usd() if locked is None else locked
        lk, lp = float(locked.get("kalshi", 0.0)), float(locked.get("pmus", 0.0))
        gross_room = float(lim.gross_usd) * frac - lk - lp
        kalshi = max(0.0, min(float(lim.per_venue_usd) * frac - lk, gross_room))
        out = {"kalshi": kalshi, "pmus": 0.0}
        if has_pmus is None:
            has_pmus = any(p.venue == "pmus" for p in self.programs.values())
        if has_pmus:
            out["pmus"] = max(0.0, min(_env_num("LIP_PMUS_BUDGET_USD", 300.0) - lp, gross_room - kalshi,
                                       float(lim.per_venue_usd) * frac - lp))
        wd_cap = _env_num("LIP_WD_MAX_CAPITAL_USD", 0.0)
        total = out["kalshi"] + out["pmus"]
        if wd_cap > 0 and total > wd_cap:
            out = {vn: usd * wd_cap / total for vn, usd in out.items()}
        return out

    def check_watchdog_capital(self) -> str | None:
        """Startup check: the most the engine could allocate (no inventory,
        PM US on) against the watchdog's LIP_WD_MAX_CAPITAL_USD. Logs a loud
        warning and returns it when the engine's own caps exceed the watchdog
        cap (allocation is then clamped to the watchdog cap)."""
        wd_cap = _env_num("LIP_WD_MAX_CAPITAL_USD", 0.0)
        if wd_cap <= 0:
            return None
        lim = self.risk.limits
        frac = alloc_cap_fraction()
        kalshi = min(float(lim.per_venue_usd), float(lim.gross_usd)) * frac
        pmus = max(0.0, min(_env_num("LIP_PMUS_BUDGET_USD", 300.0), float(lim.gross_usd) * frac - kalshi,
                            float(lim.per_venue_usd) * frac))
        if kalshi + pmus <= wd_cap:
            return None
        msg = (f"engine max budget ${kalshi + pmus:.0f} (bankroll ${self.bankroll:.0f}) exceeds "
               f"LIP_WD_MAX_CAPITAL_USD ${wd_cap:.0f}; allocation clamped to the watchdog cap. "
               "Set LIP_BANKROLL so the engine budget fits the watchdog cap.")
        logging.getLogger("lip.risk").error("!!! CAPITAL MISMATCH: %s", msg)
        self.budget_warning = msg
        return msg

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
        why = self._calendar_block(market, ts)
        if why:
            self._cancel(market, why)
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
        fv_row = self._fv_quote_row(market)
        if fv_row is None and self._fv_fail_closed(market):
            self._fv_count("unavailable")
            self._cancel(market, "fv_unavailable")
            return False
        if fv_row is not None and sides:
            # FV-driven paper quoting replaces the disagreement guard here.
            kept = self._fv_gate_sides(market, fv_row, int(yes_cents), int(no_cents), sides, size)
            if not kept:
                self._cancel(market, "fv_no_positive_side")
                return False
            sides = kept
        fv_drop = self._fv_drop(market, ts) if (sides and fv_row is None) else ()
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
        # Inventory caps act before fills: each side rests at most what it
        # could fill without breaching the market/event unpaired-$ caps.
        side_size = {}
        for sd in sides:
            room = self._side_room_contracts(market, sd, yes_cents if sd == "yes" else no_cents)
            side_size[sd] = size if room is None else float(min(size, int(room)))
        sides = tuple(sd for sd in sides if side_size[sd] >= 1)
        if not sides:
            self._cancel(market, "inventory_cap")
            return False
        self._release(market)
        add = sum((Decimal(yes_cents if sd == "yes" else no_cents) * Decimal(str(side_size[sd]))
                   for sd in sides), Decimal(0)) / Decimal(100)
        decision = self.risk.check_quote(market=market, venue=self._venue(market), add_usd=add,
                                         daily_pnl_usd=self.daily_pnl_usd(), now=ts)
        self.risk_rows.append({
            "market": market, "allowed": decision.allowed,
            "reason": decision.reason, "cancel_all": decision.cancel_all,
        })
        if not decision.allowed:
            if str(decision.reason).startswith(CAP_REASONS) and not decision.cancel_all:
                # A position/exposure cap: skip this quote, keep running.
                self._cap_skip(market, str(decision.reason))
                return False
            self._cancel(market, decision.reason)
            self._latch_kill(decision.reason, cancel_all=decision.cancel_all)
            return False
        book = self.accruals[market].book.book
        if not book.is_usable():
            # Fail closed: never place against a stale/empty/off-grid book.
            self._cancel(market, "book_unusable")
            return False
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
                                  size=side_size["yes"], opposing_bid_cents=no_bid)
            if "no" in sides:
                self.poster.place(market=market, side="no", price_cents=no_cents,
                                  size=side_size["no"], opposing_bid_cents=yes_bid)
        else:
            for side, price in (("yes", yes_cents), ("no", no_cents)):
                self.sim.untrack(f"{market}:{side}")
                if side not in sides:
                    continue
                self.sim.track(
                    order_id=f"{market}:{side}", market_ticker=market, side=side,
                    price_cents=price, size=side_size[side], book=book, now=ts,
                    program_id=market,
                )
        quote = {"yes": side_size["yes"] if "yes" in sides else 0.0,
                 "no": side_size["no"] if "no" in sides else 0.0,
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
            "market": market, "size": max(side_size[sd] for sd in sides),
            "yes_cents": yes_cents, "no_cents": no_cents,
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
        self.markouts.on_clock(ts, self._side_mid_cents, self._fv_side_cents)
        self._fv_calib_tick(ts)

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
            "fills_n": self.fills_total,
            "fills_synthetic_n": sum(self.fills_synthetic_by_venue.values()),
            "positions": self.positions_report(),
            "unresolved_positions": self.unresolved_report(),
            **self.settled_report(),
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
            "markout_horizons": self.markouts.report(),
            "fv_calibration": dict(self.fv_calib.report(), watching_n=len(self.fv_calib_watch)),
            "event_calendar": self.calendar.summary(self.now or None),
            "policy_skips": dict(__import__("collections").Counter(w for _m, w in self.policy_skips)),
            "pulls": dict(self.pulls),
            "repegs_n": self.repegs_n,
            "skew": self._skew_status(),
            "recorder": (self.recorder.summary() if getattr(self, "recorder", None) is not None
                         else {"enabled": False}),
            "pmus": (self.pmus.summary() if getattr(self, "pmus", None) is not None
                     else {"enabled": False}),
            "fair_value": (dict(self.fv.summary(), blocks=dict(self.fv_blocks),
                                withheld=sorted(k for k, v in self._fv_state.items() if v),
                                fv_quote=self.fv_quote_report())
                           if self.fv is not None else {"enabled": False}),
            "select_ms": getattr(self, "select_ms", None),
            "size_ladder": size_ladder(self.chunk),
            "estimates_partial": estimates is None,
            "session_elapsed_s": None if elapsed is None else round(elapsed, 1),
            "last_frame_ts": self.now or None,
            "kill": self.kill,
            **self.pnl_report(accrual),
            "rewards_usd": "0",
            "inventory_locked_usd": round(sum(self.locked_usd().values()), 6),
            "closed_periods_n": self.closed_periods_n,
            "closed_periods_raw_usd": round(sum(self.closed_periods.values())
                                            + sum(self.closed_periods_agg.values()), 6),
            "engine_alerts": list(self.alerts[-10:]),
            "feed": {"connected": self.connected, "disconnects_n": self.disconnects_n,
                     "last_reconnect": self.last_reconnect, "clock_skew_n": self.skew_n,
                     "clock_skew_active": self._skew_active},
            "cap_trims_n": self.cap_trims_n,
            "programs_pruned_n": self.programs_pruned_n,
            "state": {"path": self.state_path, "error": self.state_error,
                      "saved_ts": self._state_saved_at or None},
            "budget_warning": getattr(self, "budget_warning", None),
            "day": day,
        }

    def fv_quote_report(self) -> dict:
        """Status of FV-driven paper quoting: flags, markets it drives now,
        markets resting only on a fair value, withheld-side counters."""
        from mm.unattended import fairvalue as fvm
        driving = sorted(m for m in self.programs if self._fv_quote_row(m) is not None)
        return {"enabled": fvm.fv_quote_enabled() and self.mode == "paper",
                "families": list(fvm.fv_quote_families()), "min_conf": fvm.fv_min_conf(),
                "max_giveup_cents": fvm.fv_max_giveup_cents(),
                "longshot_tilt_cents": fvm.fv_longshot_tilt_cents(),
                "min_hours_to_close": fvm.fv_min_close_hours(),
                "driving_n": len(driving), "driving": driving[:20],
                "resting_driven": sorted(m for m in self.resting if m in driving)[:20],
                "admitted_on_fv": sorted(self._fv_admitted)[:20],
                "counters": dict(self.fv_quote_stats)}

    def positions_report(self) -> dict:
        """Per-market held legs for /status (the watchdog's inventory
        source): {market: {yes, no, yes_cost, no_cost}} in contracts and USD
        cost. Settled markets are left out (their legs no longer carry
        settlement risk), and so are released unresolved positions (already
        counted as a full loss in P&L; listed in ``unresolved_positions``)."""
        return {m: {k: round(float(p[k]), 6) for k in ("yes", "no", "yes_cost", "no_cost")}
                for m, p in self.position.items() if m not in self.settled and m not in self.unresolved}

    def pnl_report(self, accrual: dict | None = None) -> dict:
        """Status P&L fields. ``pnl_usd`` is an ESTIMATE: MTM markout of held
        positions + estimated LIP rewards (current windows capped at
        max_reward, plus rolled-over periods) + PM US rebates - Kalshi maker
        fees. ``premium_paid_usd`` is what paper fills cost."""
        parts = self.pnl_parts()
        by_venue = {"kalshi": 0.0, "pmus": 0.0}
        for market, v in (accrual or {}).items():
            vn = self._venue_of(market)
            by_venue[vn] = by_venue.get(vn, 0.0) + float(v.get("capped_raw_usd", v["raw_usd"]))
        for market, usd in self.closed_periods.items():
            vn = self._venue_of(market)
            by_venue[vn] = by_venue.get(vn, 0.0) + float(usd)
        for key, usd in self.closed_periods_agg.items():
            vn = key.split("/", 1)[0]
            by_venue[vn] = by_venue.get(vn, 0.0) + float(usd)
        rewards = sum(by_venue.values())
        pnl = parts["markout_usd"] + rewards + parts["rebates_usd"] - parts["fees_usd"]
        return {
            "pnl_usd": format(Decimal(str(round(pnl, 6))), "f"),
            "pnl_usd_note": ("ESTIMATE: markout (MTM at mid / one-sided / last mark / settlement) "
                             "+ estimated raw LIP rewards (not paid) + PM US rebates - Kalshi maker fees"),
            "pnl_parts": {"markout_usd": round(parts["markout_usd"], 6),
                          "est_rewards_usd": round(rewards, 6),
                          "rebates_usd": round(parts["rebates_usd"], 6),
                          "fees_usd": round(parts["fees_usd"], 6),
                          "rewards_partial": accrual is None},
            "premium_paid_usd": format(Decimal(str(round(self.premium_usd_total, 6))), "f"),
            "daily_mtm_pnl_usd": format(self.daily_pnl_usd(), "f"),
            "unsettled_positions": parts["unsettled"][:50],
            "unmarked_positions": parts["unmarked"][:50],
            "pnl_attribution": self.pnl_attribution(parts, by_venue, pnl),
        }

    def pnl_attribution(self, parts: dict, rewards_by_venue: dict, pnl: float) -> dict:
        """Split the estimated ``pnl_usd`` into parts that sum to it.

        spread_capture_usd    sum of count x (side mark at fill - fill price)
        adverse_selection_usd sum of count x (side mark 10 min after the fill
                              - side mark at fill), fills whose 10 min check
                              was measured on time (mm/unattended/markouts.py)
        inventory_mtm_usd     the rest of the position markout: later mark
                              moves and settlement, plus fills with no mark at
                              fill or no on-time 10 min check
        est_rewards_*_usd     estimated LIP rewards (Kalshi) / PM US liquidity
                              rewards, current windows capped + rolled periods
        rebates_usd           PM US maker rebates
        fees_usd              Kalshi maker fees, as a negative number
        Every figure is an ESTIMATE from paper fills and modelled rewards."""
        spread = float(self.markouts.spread_usd)
        adverse = float(self.markouts.adverse_usd)
        out = {
            "label": "estimate (paper): attribution of pnl_usd from simulated fills and "
                     "estimated (unpaid) rewards; components sum to total_usd",
            "spread_capture_usd": round(spread, 6),
            "adverse_selection_usd": round(adverse, 6),
            "inventory_mtm_usd": round(parts["markout_usd"] - spread - adverse, 6),
            "est_rewards_kalshi_usd": round(float(rewards_by_venue.get("kalshi", 0.0)), 6),
            "est_rewards_pmus_usd": round(float(rewards_by_venue.get("pmus", 0.0)), 6),
            "rebates_usd": round(parts["rebates_usd"], 6),
            "fees_usd": round(-parts["fees_usd"], 6),
            "total_usd": round(pnl, 6),
            "spread_unmeasured_fills": self.markouts.spread_unmeasured,
            "adverse_measured_fills": self.markouts.adverse_n,
        }
        return out

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
                "fills_n": int(self.fills_by_venue.get(vn, 0)),
                "synthetic_fills_n": int(self.fills_synthetic_by_venue.get(vn, 0)),
                "plan_net_usd_per_day": round(sum(float((self.last_plan.get(m) or {}).get("value_per_day") or 0.0)
                                                  for m in mk), 4),
            }
        out["pmus"]["rebates_usd"] = round(self.pm_rebate_usd, 4)
        out["pmus"]["fill_fidelity"] = (
            "low: no public PM US trade tape; paper fills come from prints inferred from two "
            "book polls and from polled books crossing our price (synthetic_fills_n)")
        out["pmus"]["screen"] = getattr(self, "pmus_screen", None)
        out["ext_frames_n"] = self.ext_frames_n
        out["refeeds_n"] = self.refeeds_n
        return out

    def _side_mid_cents(self, market: str, side: str):
        """Mark of one side in cents: book mid, the remaining side of a
        one-sided book, the last known mark, or the settlement value."""
        yes_mark, _src = self._yes_mark(market)
        if yes_mark is None:
            return None
        return yes_mark if side == "yes" else 100.0 - yes_mark

    def bucket_report(self, accrual: dict | None = None) -> dict:
        """Per-bucket capital, est rewards, fills, premium, markout, fees, pnl.
        Read-only apart from refreshing marks.

        Fills are attributed to the bucket the market had when it filled.
        markout_usd = MTM of the bucket's positions at their marks (mid,
        remaining side of a one-sided book, last known mark, or settlement)
        minus cost. raw_est_usd = estimated raw rewards of current windows
        (capped at max_reward) plus rolled-over periods. fees_usd = Kalshi
        maker fees charged on paper fills. pnl_usd = markout + raw_est + PM US
        rebates - fees (an estimate: rewards are not paid figures). A released
        unresolved position is valued at 0 (a full loss of its cost). Settled
        positions already dropped count through ``bucket_closed``; ended
        markets' closed periods through ``closed_periods_agg``."""
        tags = getattr(self, "bucket_of", {}) or {}
        out = {b: {"budget_usd": round(float((getattr(self, "bucket_budget", None) or {}).get(b, 0.0)), 2),
                   "selected_n": 0, "selected": [], "capital_usd": 0.0, "raw_est_usd": 0.0,
                   "fills_n": 0, "synthetic_fills_n": 0, "premium_usd": 0.0, "markout_usd": 0.0,
                   "fees_usd": 0.0,
                   "rebates_usd": 0.0, "pnl_usd": 0.0}
               for b in ("durable", "short")}
        for market in self.resting:
            b = tags.get(market, "short")
            out[b]["selected_n"] += 1
            out[b]["selected"].append(market)
            out[b]["capital_usd"] += float(self.committed.get(market, 0) or 0)
        for market, info in (accrual or {}).items():
            b = tags.get(market)
            if b in out:
                out[b]["raw_est_usd"] += float(info.get("capped_raw_usd", info["raw_usd"]))
        for market, usd in self.closed_periods.items():
            b = tags.get(market)
            if b in out:
                out[b]["raw_est_usd"] += float(usd)
        for key, usd in self.closed_periods_agg.items():
            b = key.split("/", 1)[1] if "/" in key else ""
            if b in out:
                out[b]["raw_est_usd"] += float(usd)
        for b, agg in self.bucket_closed.items():
            if b not in out:
                continue
            out[b]["fills_n"] += int(agg.get("fills_n", 0))
            out[b]["synthetic_fills_n"] += int(agg.get("synthetic_n", 0))
            for k in ("premium_usd", "markout_usd", "fees_usd", "rebates_usd"):
                out[b][k] += float(agg.get(k, 0.0))
        for b, rows in self.bucket_pos.items():
            if b not in out:
                continue
            for market, pos in rows.items():
                cost = float(pos["yes_cost"]) + float(pos["no_cost"])
                mark, _src = self._yes_mark(market)
                value = 0.0 if (mark is None or market in self.unresolved) else (
                    float(pos["yes"]) * mark + float(pos["no"]) * (100.0 - mark)) / 100.0
                out[b]["fills_n"] += int(pos.get("fills_n", 0))
                out[b]["synthetic_fills_n"] += int(pos.get("synthetic_n", 0))
                out[b]["premium_usd"] += cost
                out[b]["markout_usd"] += value - cost
                out[b]["fees_usd"] += float(pos.get("fees", 0.0))
                out[b]["rebates_usd"] += float(pos.get("rebates", 0.0))
        for b in out.values():
            b["pnl_usd"] = b["markout_usd"] + b["raw_est_usd"] + b["rebates_usd"] - b["fees_usd"]
            for k in ("capital_usd", "raw_est_usd", "premium_usd", "markout_usd", "fees_usd",
                      "rebates_usd", "pnl_usd"):
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
            raw = Decimal(est.raw_usd)
            out[market] = {"raw_usd": raw,
                           "capped_raw_usd": raw if acc.max_reward_usd is None else min(raw, acc.max_reward_usd),
                           "known": est.known_seconds,
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
        all_accrual = self.live_accrual()
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
            "fills_n": self.fills_total,
            "estimated_usd": format(estimated, "f"),
            "estimates": {market: format(amount, "f") for market, amount in estimates.items()},
            "inferred": inferred,
            # Balance-residual (inferred) credits are reported but never move
            # a series factor (engine.lip_calibration.series_factors skips
            # them): this counts the observations left out of ``factors``.
            "inferred_credits_excluded_n": sum(1 for obs in observations if obs.inferred),
            "factors": factors,
            "reconcile": {
                "matches": [row.ratio for row in matched["matches"]],
                "rejected": len(rejected),
            },
            "next_usd": next_usd,
            "risk": self.risk_rows,
            "kill": self.kill,
            **self.pnl_report(all_accrual),
            "buckets": self.bucket_report(all_accrual),
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
            "max_reward_usd": parsed.get("max_reward_usd"),
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


def default_fill_cap_usd() -> float:
    """Per-order single-fill cap. LIP_SINGLE_FILL_CAP_USD when set; unset, the
    session default ($100) lowered to LIP_MARKET_INV_CAP_USD when that cap is
    set, so one fill can never exceed the per-market inventory cap."""
    cap = single_fill_cap_usd()
    if str(os.environ.get("LIP_SINGLE_FILL_CAP_USD", "")).strip():
        return cap
    inv = _env_num("LIP_MARKET_INV_CAP_USD", 0.0)
    return min(cap, inv) if inv > 0 else cap


def pmus_market_cap_usd() -> float:
    """LIP_PMUS_MARKET_CAP_USD: per-market capital cap for PM US (PMUS:*)
    quotes in sizing. Unset or <= 0: no extra cap."""
    cap = _env_num("LIP_PMUS_MARKET_CAP_USD", 0.0)
    return cap if cap > 0 else float("inf")


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
                               meta=None, settle_candidates: Callable | None = None,
                               paper: bool = False) -> None:
    """Production books and public trades. The reader cannot place an order.
    ``paper`` (False: fail closed) lets the screen feed model families early
    (screen.fv_early_feed); the service passes True only in paper mode.

    Startup: load programs, screen them against the on-disk metadata cache,
    connect the websocket for the cached candidates, then fetch missing
    market metadata (GET /markets?tickers=..., batched) and series
    categories in the background and add new candidates. Programs are
    re-checked every ``refresh_s``. Only the top ``LIP_CANDIDATE_TOP``
    candidates are fed to the loop and subscribed. Each refresh also
    backfills settlements for ``settle_candidates()`` (held Kalshi positions
    past close; ``_settlement_backfill``). Transient HTTP/network/
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
           "lock": threading.Lock(), "settle_candidates": settle_candidates, "paper": bool(paper)}
    backoff = READONLY_BACKOFF_START_S
    attempts = 0
    last_wall = None
    down_since = None
    try:
        while True:
            attempts += 1
            state = {"frames": False}
            if down_since is not None:
                # Same RunLoop across reconnects; the risk engine sees how long
                # the feed was blind (since the last frame, or the drop).
                now = time.time()
                on_frame({"kind": "reconnect", "ts": now,
                          "stale_s": now - (last_wall if last_wall is not None else down_since)})
            try:
                await _readonly_books_session(source, key, session, on_frame, state, ctx=ctx)
                exc = None
                reason = "clean_close"
            except transient as err:
                exc = err
                reason = type(err).__name__
            if state.get("last_wall") is not None:
                last_wall = state["last_wall"]
            if state["frames"]:
                backoff = READONLY_BACKOFF_START_S
            # Transient error, sequence gap, or a clean close (1000/1001 ends
            # ``async for`` without an error): mark every book disconnected
            # in the SAME loop and reconnect. Never a fresh RunLoop.
            down_since = time.time()
            on_frame({"kind": "disconnect", "ts": down_since, "reason": reason})
            log.warning(
                "read-only books %s (%s); reconnect in %.0fs", reason,
                "" if exc is None else str(exc)[:200], backoff,
            )
            if max_attempts is not None and attempts >= max_attempts:
                if exc is not None:
                    raise exc
                return
            await nap(backoff)
            backoff = min(backoff * 2.0, READONLY_BACKOFF_MAX_S)
    finally:
        session.close()


def _exchange_ts(msg: dict):
    """Venue time (epoch s) of a Kalshi websocket frame: top-level
    ``sending_ts_ms`` (when Kalshi queued the message, snapshots and deltas),
    else the delta's ``msg.ts_ms`` (when the change was recorded). None when
    absent. See RunLoop._note_clock_skew."""
    raw = msg.get("sending_ts_ms")
    if raw is None:
        raw = (msg.get("msg") or {}).get("ts_ms") if isinstance(msg.get("msg"), dict) else None
    try:
        return None if raw is None else float(raw) / 1000.0
    except (TypeError, ValueError):
        return None


def _program_sig(frame: dict) -> tuple:
    return tuple(frame.get(k) for k in (
        "program_id", "start_ts", "end_ts", "period_reward_usd", "period_seconds", "target_size",
        "discount_factor", "fee_type", "fee_multiplier", "category", "close_ts", "occurrence_ts",
        "max_reward_usd"))


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


PRUNE_GRACE_S = 3600.0


def _prune_fed(candidates: list[dict], ctx: dict, on_frame: Callable[[dict], None],
               now: float | None = None) -> list[str]:
    """Drop fed programs that ended more than PRUNE_GRACE_S ago and are not
    among the current candidates (a roll-over re-feed keeps them): send the
    loop a ``program_end`` frame and forget them here, so a reconnect does not
    resubscribe them and a later return is fed (and subscribed) as new. The
    pruned tickers are queued in ``ctx["unsubscribe_pending"]``; the
    background refresh removes them from the live subscription
    (``_flush_unsubscribes``), and the loop ignores their frames meanwhile."""
    now = time.time() if now is None else float(now)
    ends = ctx.setdefault("ends", {})
    current = set()
    for frame in candidates:
        market = frame.get("market")
        current.add(market)
        if frame.get("end_ts") is not None:
            ends[market] = float(frame["end_ts"])
    gone = [m for m in list(ctx["fed"]) if m not in current
            and ends.get(m) is not None and ends[m] + PRUNE_GRACE_S < now]
    for market in gone:
        on_frame({"kind": "program_end", "market": market, "ts": now, "reason": "program_ended"})
        ctx["fed"].pop(market, None)
        ctx.get("sigs", {}).pop(market, None)
        ends.pop(market, None)
    if gone:
        pending = ctx.setdefault("unsubscribe_pending", [])
        pending.extend(gone)
        del pending[:-5000]
    return gone


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
        missing = needs_series(programs, meta, paper=bool(ctx.get("paper"))) + meta.stale_series(
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
        candidates, stats = screen(ctx["programs"], ctx["meta"], paper=bool(ctx.get("paper")))
    finally:
        ctx["lock"].release()
    new = _feed_programs(candidates, ctx["fed"], on_frame, ctx.setdefault("sigs", {}))
    stats["pruned"] = len(_prune_fed(candidates, ctx, on_frame))
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
    from mm.venues.readonly import TICKER_WS_CHANNELS
    names = sorted(tickers)
    if not names:  # an empty ticker list must never become a subscribe-to-everything
        return
    for i in range(0, len(names), 100):
        await sock.subscribe(sorted(TICKER_WS_CHANNELS), names[i:i + 100])


async def _subscribe_lifecycle(sock) -> None:
    """market_lifecycle_v2 (all markets; no ticker filter exists) so
    determined/settled results reach RunLoop.settle. Once per connection."""
    from mm.venues.readonly import LIFECYCLE_WS_CHANNEL
    await sock.subscribe([LIFECYCLE_WS_CHANNEL])


async def _flush_unsubscribes(sock, ctx: dict) -> None:
    """Remove pruned tickers (``_prune_fed``) from the live subscription."""
    pending = ctx.get("unsubscribe_pending") or []
    if not pending:
        return
    names = sorted(set(pending))
    ctx["unsubscribe_pending"] = []
    unsub = getattr(sock, "unsubscribe_markets", None)
    if unsub is not None:
        await unsub(names)


WS_RAW_TYPE = "ws_raw"


def _ws_raw_row(kind: str, msg: dict) -> dict:
    """A raw websocket message as an evidence row for the frame recorder
    (tools/verify_ws_frames.py). RunLoop.on_frame ignores this type."""
    return {"type": WS_RAW_TYPE, "ts": msg.get("ts"), "channel": kind, "msg": dict(msg)}


def _dispatch_ws_message(msg: dict, on_frame: Callable[[dict], None], seqr: "SidSequencer",
                         sock) -> None:
    """Route one decoded read-only websocket message. Raises SequenceGap on a
    real book sequence gap.

    Book frames reach ``on_frame`` with ``seq`` set to None (SidSequencer has
    checked it per subscription) and the original kept as ``ws_seq``. Raw
    market_lifecycle_v2 messages and subscribed/unsubscribed/ok replies are
    also passed on as ``{"type": "ws_raw", "channel": <type>, "msg": <message>}``
    rows: RunLoop ignores them, the recorder keeps them as evidence. A real
    gap is recorded first as a ``seq_gap`` ws_raw row (sid, seq, last_seq,
    market_ticker) before SequenceGap is raised."""
    kind = str(msg.get("type") or "")
    if kind == "trade":
        body = msg.get("msg") or msg
        on_frame({"type": "trade", "ts": msg["ts"], "trade": body})
    elif kind == "market_lifecycle_v2":
        on_frame(_ws_raw_row(kind, msg))
        # A determined/settled result settles paper inventory at 100/0
        # (RunLoop.settle ignores markets it holds nothing in).
        body = msg.get("msg") or {}
        result = str(body.get("result") or "").lower()
        if body.get("event_type") in ("determined", "settled") and result in ("yes", "no"):
            on_frame({"kind": "settlement", "ts": msg["ts"],
                      "market": body.get("market_ticker"), "result": result})
    elif kind in ("orderbook_snapshot", "orderbook_delta"):
        prev = seqr.last.get(msg.get("sid"))
        verdict = seqr.check(msg)
        if verdict == "dup":
            return
        if verdict == "gap":
            # Evidence row for tools/verify_ws_frames.py (the gapped frame is
            # not applied; the session reconnects).
            on_frame(_ws_raw_row("seq_gap", {
                "type": "seq_gap", "ts": msg.get("ts"), "sid": msg.get("sid"), "seq": msg.get("seq"),
                "last_seq": prev, "frame_type": kind,
                "market_ticker": (msg.get("msg") or {}).get("market_ticker")}))
            raise SequenceGap(f"sid {msg.get('sid')} sequence gap at {msg.get('seq')}")
        msg["ws_seq"] = msg.get("seq")
        msg["seq"] = None
        on_frame(msg)
    elif kind in ("subscribed", "unsubscribed", "ok"):
        if sock is not None and hasattr(sock, "note_response"):
            sock.note_response(msg)
        if kind != "subscribed":
            # update_subscription / unsubscribe acks carry the subscription's
            # seq; record it so the next book message is not seen as a gap.
            seqr.check(msg)
        on_frame(_ws_raw_row(kind, msg))


KALSHI_FINAL_STATUSES = ("determined", "amended", "finalized", "settled")


def kalshi_market_result(payload: dict) -> str | None:
    """"yes"/"no" from a GET /markets/{ticker} payload once the outcome is
    final (status determined/amended/finalized, or legacy settled; result
    yes/no). None otherwise: open/closed/disputed markets, scalar results.
    docs.kalshi.com/api-reference/market/get-market."""
    mk = payload.get("market") if isinstance(payload.get("market"), dict) else payload
    status = str((mk or {}).get("status") or "").lower()
    result = str((mk or {}).get("result") or "").lower()
    if status in KALSHI_FINAL_STATUSES and result in ("yes", "no"):
        return result
    return None


def _backfill_fetch(reader, tickers: list[str]) -> list[tuple[str, str]]:
    """Thread body: read-only GET /markets/{ticker} per ticker. One
    ticker's HTTP error is logged and skipped (retried next refresh)."""
    from urllib.parse import quote
    from mm.venues.readonly import ReadOnlyHTTPError
    log = logging.getLogger("lip.readonly")
    out = []
    for ticker in tickers:
        try:
            payload = reader.get(f"/markets/{quote(ticker, safe='')}")
        except ReadOnlyHTTPError as exc:
            log.warning("settlement backfill %s HTTP %s", ticker, exc.status)
            continue
        result = kalshi_market_result(payload if isinstance(payload, dict) else {})
        if result is not None:
            out.append((ticker, result))
    return out


async def _settlement_backfill(reader, ctx: dict, on_frame, now: float | None = None) -> int:
    """Kalshi settlements missed while the socket was down or disconnected:
    ask GET /markets/{ticker} (read-only, GET only) for held positions past
    close or with no program left, then markets with pending fair-value
    calibration samples past close (``ctx["settle_candidates"]``: the loop's
    ``settle_view`` and ``fv_settle_view``), at most LIP_SETTLE_BACKFILL_MAX (50) tickers per
    refresh, each at most once per LIP_SETTLE_POLL_S (600 s). A final yes/no
    result is booked through the loop's ``settlement`` frame (RunLoop.settle).
    Returns the number booked."""
    import asyncio
    fn = ctx.get("settle_candidates")
    if fn is None:
        return 0
    now = time.time() if now is None else float(now)
    seen = ctx.setdefault("settle_checked", {})
    every = _env_num("LIP_SETTLE_POLL_S", 600.0)
    due = [t for t in dict.fromkeys(fn() or []) if t and now - seen.get(t, -1e18) >= every]
    due = due[: max(1, int(_env_num("LIP_SETTLE_BACKFILL_MAX", 50)))]
    if not due:
        return 0
    for t in due:
        seen[t] = now
    for t in [t for t, at in seen.items() if now - at > 7 * 86400.0]:
        del seen[t]
    found = await asyncio.to_thread(_backfill_fetch, reader, due)
    for ticker, result in found:
        on_frame({"kind": "settlement", "ts": now, "market": ticker, "result": result,
                  "source": "rest_backfill"})
    if found:
        logging.getLogger("lip.readonly").info("settlement backfill booked %d of %d", len(found), len(due))
    return len(found)


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
            await _flush_unsubscribes(sock, ctx)
            await _settlement_backfill(reader, ctx, on_frame)
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
        # a fresh connection subscribes only what is fed now
        ctx["unsubscribe_pending"] = []
        await _subscribe(sock, ctx["fed"])
        await _subscribe_lifecycle(sock)
        task = asyncio.create_task(_background(reader, ctx, on_frame, sock, state))
        seqr = SidSequencer()
        try:
            async for raw in sock._ws:
                state["frames"] = True
                msg = json.loads(raw)
                msg.setdefault("ts", time.time())
                state["last_wall"] = msg["ts"]
                ets = _exchange_ts(msg)
                if ets is not None:
                    msg["exchange_ts"] = ets
                _dispatch_ws_message(msg, on_frame, seqr, sock)
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
            "max_reward_usd": parsed.get("max_reward_usd"),
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
        "premium_paid_usd": "0",
        "rewards_usd": "0",
        "day": datetime.now(timezone.utc).date().isoformat(),
        "kill": None,
        "ws_url": ws_url,
        "data_source": books["flag"],
    }
