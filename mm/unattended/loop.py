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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from engine.lip_accrual import RestingOrder, SecondAccrual
from engine.lip_calibration import RatioObs, series_factors
from engine.lip_reconcile import (
    EstimateRow, credits_from_ledger, infer_reward_credits, reconcile,
)
from engine.lip_scorer import ProgramParams
from execution.paper_fills import PaperFillSimulator
from mm.bankroll import capital_usd
from mm.compound import MarketSample, reallocate
from mm.ops import skew_is_excessive
from mm.risk import FillClock, Limits, RiskEngine
from mm.selector import KalshiMarket, allocate, expected_net_per_dollar, reward_per_day
from mm.session_gates import (
    candidate_top, clamp_contracts, inside_close_window, plan_is_suspect,
    plan_per_hundred, pull_before_close_s, single_fill_cap_usd,
)
from mm.unattended.feed import DEMO_WS_URL
from mm.unattended.optimize import optimize_sizes
from mm.unattended.service import UnattendedRefused

_log = logging.getLogger("lip.readonly")
# Pause after a failed production-book read before the service tries again.
READONLY_DATA_BACKOFF_S = 5.0
from mm.venues.kalshi_rest import DEMO_HOSTS, PRODUCTION_HOSTS

SELECT_EVERY_S = 600.0
# Size up to this fraction of the risk per-venue cap. The last 5% is slack
# so a tick of rounding does not trip the hard cap.
VENUE_HEADROOM = 0.95
# Share used when ranking names that do not have a book yet.
CANDIDATE_SIZE = 100.0
# Live loop scores one completed second per tick while quotes rest.
CLOCK_INTERVAL_S = 1.0


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
    category: str = ""


class RunLoop:
    """One pass over recorded or live frames. The constructor opens nothing."""

    def __init__(self, *, mode: str = "paper", bankroll: float | None = None,
                 select_every: float = SELECT_EVERY_S,
                 chunk: float = 100.0,
                 poster: DemoPoster | None = None,
                 series_stats: dict | None = None,
                 latency_ms: float = 250.0,
                 pull_before_s: float | None = None,
                 fill_cap: float | None = None) -> None:
        if mode not in ("paper", "demo"):
            raise UnattendedRefused("live trading is not armed")
        self.mode = mode
        # None follows LIP_BANKROLL (then LIP_ACCOUNT_USD, then $5,000).
        # A caller that passes a number is sizing a fixture, not the account.
        self.bankroll = float(capital_usd() if bankroll is None else bankroll)
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
        self.kill = None
        self.now = 0.0
        self.socket_opened = False
        self.books_seen: set[str] = set()
        self._books_at_last_select: set[str] = set()
        self.suspect_markets: list[dict] = []

    def add_program(self, row: dict) -> None:
        market = str(row["market"])
        start = float(row.get("start_ts") or 0)
        period = float(row.get("period_seconds") or 86400)
        end = float(row["end_ts"]) if row.get("end_ts") is not None else start + period
        close = None if row.get("close_ts") is None else float(row["close_ts"])
        days = None if row.get("days_to_settle") is None else float(row["days_to_settle"])
        shard = row.get("exchange_index")
        existing = self.programs.get(market)
        if existing is not None:
            if shard is not None:
                existing.exchange_index = int(shard)
            if close is not None:
                existing.close_ts = close
            if days is not None:
                existing.days_to_settle = days
            if row.get("category"):
                existing.category = str(row["category"])
            return
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
            category=str(row.get("category") or ""),
        )
        self.programs[market] = prog
        params = ProgramParams(
            market_ticker=market,
            target_size=prog.target_size,
            discount_factor=prog.discount_factor,
            period_reward_usd=prog.period_reward_usd,
            program_id=str(row.get("program_id") or market),
            period_seconds=prog.period_seconds,
            start_ts=prog.start_ts,
            end_ts=prog.end_ts,
        )
        self.accruals[market] = SecondAccrual(params, series=prog.series)
        self.open_seconds.setdefault(market, None)

    def on_frame(self, row: dict) -> None:
        kind = str(row.get("kind") or row.get("type") or "")
        if kind == "program":
            self.add_program(row)
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
        """Score the open second once the clock moves past it.

        A one-second step leaves the new second open, so a resting quote
        keeps accruing on the live clock. A jump marks the interior
        missed: those seconds were not observed, and they are not filled
        in from the last book. The current second stays unscored.
        """
        for market, accrual in self.accruals.items():
            open_s = self.open_seconds.get(market)
            if open_s is not None and open_s < second:
                accrual.score_second(open_s)
            accrual.omit_until(second)
            if market in self.books_seen and (open_s is None or second <= open_s + 1):
                self.open_seconds[market] = second
            elif open_s is not None and open_s < second:
                self.open_seconds[market] = None

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
        self.books_seen.add(market)

    def _on_trade(self, row: dict, ts: float) -> None:
        trade = dict(row.get("trade") or row.get("msg") or row)
        trade.setdefault("created_time", _iso(ts))
        trade.setdefault("ticker", trade.get("market_ticker") or "")
        if not trade.get("trade_id"):
            return
        for fill in self.sim.apply_trades([trade]):
            self.fills.append(fill)
            self._reduce_resting(fill)
            decision = self.risk.record_fill(1, now=ts)
            if not decision.allowed:
                self.kill = {"reason": decision.reason, "cancel_all": decision.cancel_all,
                             "paper": self.mode == "paper"}
                self._cancel_all(decision.reason)

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

    def _maybe_select(self, ts: float) -> None:
        if not self.programs or not self.books_seen:
            return
        fresh = self.books_seen - self._books_at_last_select
        if self.last_select_ts is not None and ts - self.last_select_ts < self.select_every:
            if not fresh or ts - self.last_select_ts < 1.0:
                return
        self._select(ts)
        self._books_at_last_select = set(self.books_seen)

    def _markets(self) -> list[KalshiMarket]:
        rows = []
        for market, prog in self.programs.items():
            if market not in self.books_seen:
                continue
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
                days_to_settle=prog.days_to_settle,
                exchange_index=prog.exchange_index,
                shard_cash_usd=prog.shard_cash_usd,
                category=prog.category,
            ))
        return rows

    def _venue_budget(self) -> float:
        return float(self.risk.limits.per_venue_usd) * VENUE_HEADROOM

    def _select(self, ts: float) -> None:
        self.selection_count += 1
        self.last_select_ts = ts
        markets = self._markets()
        live = self.mode != "paper"
        limits = self.risk.limits
        venue_budget = self._venue_budget()
        selection = allocate(
            markets, bankroll=self.bankroll, chunk=self.chunk, max_size=self.chunk,
            per_market_usd=float(limits.per_market_usd),
            per_series_usd=float(limits.per_series_usd),
            per_category_usd=float(limits.per_venue_usd),
            per_venue_usd=venue_budget,
            live=live, series_stats=self.series_stats,
            single_fill_cap_usd=self.fill_cap,
        )
        self.excluded = list(selection.excluded)
        sized = optimize_sizes(
            markets, bankroll=self.bankroll,
            per_market_usd=float(limits.per_market_usd),
            per_event_usd=float(limits.per_series_usd),
            total_usd=venue_budget,
            sizes=(self.chunk,), markout_usd_per_contract=0.0,
            single_fill_cap_usd=self.fill_cap,
        )
        chosen = {row.market: row for row in sized.chosen}
        taken = {row.market for row in selection.taken}
        by_market = {row.market: row for row in markets}
        taken_rows = {row.market: row for row in selection.taken}
        self.suspect_markets = []
        for market in self.programs:
            if market not in self.books_seen:
                continue
            row = chosen.get(market)
            if row is None or market not in taken or row.size <= 0:
                self._cancel(market, "not_selected")
                continue
            picked = taken_rows.get(market)
            km = by_market.get(market)
            if picked is not None and km is not None:
                planned = reward_per_day(picked.share, km)
                if plan_is_suspect(planned, picked.capital_usd):
                    self.suspect_markets.append({
                        "market": market,
                        "planned_usd_day": float(f"{planned:.4f}"),
                        "capital_usd": float(f"{picked.capital_usd:.4f}"),
                        "usd_per_100_day": float(
                            f"{plan_per_hundred(planned, picked.capital_usd):.4f}"
                        ),
                    })
            self._quote(market, int(row.yes_cents), int(row.no_cents), float(row.size), ts)

    def _inside_close(self, market: str, ts: float) -> bool:
        close_ts = self.programs[market].close_ts
        if close_ts is None:
            return False
        return inside_close_window(close_ts, ts, pull_before_s=self.pull_before_s)

    def _quote(self, market: str, yes_cents: int, no_cents: int, size: float, ts: float) -> None:
        if self.kill is not None:
            self._cancel(market, self.kill["reason"])
            return
        if self._inside_close(market, ts):
            self._cancel(market, "close_cutoff")
            return
        size = float(min(
            clamp_contracts(yes_cents, size, self.fill_cap),
            clamp_contracts(no_cents, size, self.fill_cap),
        ))
        if size <= 0:
            self._cancel(market, "fill_cap")
            return
        self._release(market)
        add = (Decimal(yes_cents) + Decimal(no_cents)) / Decimal(100) * Decimal(str(size))
        decision = self.risk.check_quote(market=market, venue="kalshi", add_usd=add, now=ts)
        self.risk_rows.append({
            "market": market, "allowed": decision.allowed,
            "reason": decision.reason, "cancel_all": decision.cancel_all,
        })
        if not decision.allowed:
            # A cap (per-market, per-series, per-venue, gross) is a skip.
            # It does not latch. A kill (cancel_all) still pulls every
            # resting quote. The latch lives on this process only; a new
            # RunLoop starts clear.
            if decision.cancel_all or self.risk.killed:
                self.kill = {
                    "reason": decision.reason, "cancel_all": True,
                    "paper": self.mode == "paper",
                }
                self._cancel(market, decision.reason)
                self._cancel_all(decision.reason)
                return
            _log.warning("skip quote %s: %s", market, decision.reason)
            self._cancel(market, decision.reason)
            return
        book = self.accruals[market].book.book
        if self.mode == "demo":
            if self.poster is None:
                raise UnattendedRefused("demo mode would send without a sender")
            no_bid = book.no_bids[0].price_cents if book.no_bids else 0
            yes_bid = book.yes_bids[0].price_cents if book.yes_bids else 0
            yes_cents = _passive_cents(yes_cents, no_bid)
            no_cents = _passive_cents(no_cents, yes_bid)
            if yes_cents <= 0 or no_cents <= 0:
                self._cancel(market, "would_cross")
                return
            self.poster.place(market=market, side="yes", price_cents=yes_cents,
                              size=size, opposing_bid_cents=no_bid)
            self.poster.place(market=market, side="no", price_cents=no_cents,
                              size=size, opposing_bid_cents=yes_bid)
        else:
            for side, price in (("yes", yes_cents), ("no", no_cents)):
                self.sim.untrack(f"{market}:{side}")
                self.sim.track(
                    order_id=f"{market}:{side}", market_ticker=market, side=side,
                    price_cents=price, size=size, book=book, now=ts,
                    program_id=market,
                )
        quote = {"yes": size, "no": size, "yes_cents": yes_cents, "no_cents": no_cents}
        self.resting[market] = quote
        self.accruals[market].set_resting(self._orders(market, quote))
        self.risk.commit(market, "kalshi", add)
        self.committed[market] = add
        self.quotes.append({
            "market": market, "size": size, "yes_cents": yes_cents, "no_cents": no_cents,
            "paper": self.mode == "paper", "ts": ts,
        })

    def _pull(self, ts: float) -> None:
        for market in list(self.programs):
            if self._inside_close(market, ts):
                self._cancel(market, "close_cutoff")

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

    def _cancel_all(self, reason: str) -> None:
        for market in list(self.resting):
            self._cancel(market, reason)

    def _release(self, market: str) -> None:
        prev = self.committed.pop(market, Decimal(0))
        if prev == 0:
            return
        self.risk.market_usd[market] = Decimal(str(self.risk.market_usd.get(market, 0))) - prev
        self.risk.venue_usd["kalshi"] = Decimal(str(self.risk.venue_usd.get("kalshi", 0))) - prev

    def live_status(self) -> dict:
        """Counts for a socket that is still open.

        Completed seconds are scored through ``self.now``. The open
        second is not scored. ``estimated_usd`` is the raw accrual
        (share × pool / period), so a partial period under the $1
        settlement floor is still visible. ``finish`` is what applies
        that floor, reconciles, and reallocates.
        """
        if self.now:
            self._close_elapsed(int(self.now))
        estimated = sum(
            (accrual.raw_usd() for accrual in self.accruals.values()),
            Decimal(0),
        )
        day = datetime.fromtimestamp(self.now or 0, timezone.utc).date().isoformat()
        from mm.venues.readonly import book_source
        books = book_source(force_demo=self.mode != "paper")
        selected = list(self.resting)
        return {
            "paper": self.mode == "paper",
            "demo": self.mode == "demo",
            "mode": self.mode,
            "live_armed": False,
            "socket_opened": self.socket_opened,
            "stage": "running",
            "programs_loaded": len(self.programs),
            "selection_count": self.selection_count,
            "markets": selected,
            "quotes": [dict(row) for row in self.quotes],
            "quotes_n": len(self.quotes),
            "resting": {market: dict(quote) for market, quote in self.resting.items()},
            "resting_n": len(self.resting),
            "fills_n": len(self.fills),
            "estimated_usd": format(estimated, "f"),
            "kill": None if self.kill is None else dict(self.kill),
            "suspect": bool(self.suspect_markets),
            "suspect_markets": [dict(row) for row in self.suspect_markets],
            "pnl_usd": "0",
            "rewards_usd": "0",
            "day": day,
            "data_source": books["flag"],
        }

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
            "suspect": bool(self.suspect_markets),
            "suspect_markets": [dict(row) for row in self.suspect_markets],
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


async def _emit_clock(on_frame: Callable[[dict], None]) -> None:
    """One clock frame a second so a quiet book still accrues and refreshes."""
    import asyncio
    try:
        while True:
            await asyncio.sleep(CLOCK_INTERVAL_S)
            on_frame({"type": "clock", "ts": time.time()})
    except asyncio.CancelledError:
        return


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
    import asyncio
    clock = asyncio.create_task(_emit_clock(on_frame))
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
        clock.cancel()
        await clock
        await ws.close()


# Ten market reads per second. The incentive list is one page of about
# 100, and each ticker needs GET /markets/{ticker} for its shard. The
# cache keeps a reconnect from repeating those reads.
MARKET_READS_PER_SECOND = 10.0
MAX_INCENTIVE_PAGES = 100


class ShardLookup:
    """Cached ``exchange_index`` from GET /markets/{ticker}.

    A cache hit does not spend the read budget. A data error is not
    cached, so the next pass can try that ticker again.
    """

    def __init__(self, *, per_second: float = MARKET_READS_PER_SECOND) -> None:
        from mm.venues.base import RateBudget
        self.per_second = float(per_second)
        self.budget = RateBudget(capacity=self.per_second, per_second=self.per_second)
        self.cache: dict[str, int] = {}

    def exchange_index(self, reader, ticker: str) -> int | None:
        from mm.venues.kalshi import exchange_index_from_market_payload
        from mm.venues.readonly import ReadOnlyDataError
        from urllib.parse import quote
        key = str(ticker)
        if key in self.cache:
            return self.cache[key]
        self._acquire()
        try:
            payload = reader.get("/markets/" + quote(key, safe=""))
        except ReadOnlyDataError as exc:
            _log.warning("read-only market %s shard lookup failed (%s)", key, exc)
            return None
        idx = exchange_index_from_market_payload(payload)
        if idx is None:
            return None
        self.cache[key] = int(idx)
        return self.cache[key]

    def _acquire(self) -> None:
        while True:
            if self.budget.allow(1.0, time.monotonic()):
                return
            gap = 1.0 / self.per_second if self.per_second > 0 else 0.05
            time.sleep(gap)


_shards = ShardLookup()


def reset_readonly_shard_cache() -> None:
    global _shards
    _shards = ShardLookup()


def _incentive_rows(reader) -> list[dict]:
    """Every active liquidity program, following ``next_cursor``."""
    rows: list[dict] = []
    cursor = ""
    seen: set[str] = set()
    for _page in range(MAX_INCENTIVE_PAGES):
        params = {"status": "active", "type": "liquidity", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = reader.get("/incentive_programs", params=params)
        rows.extend(payload.get("incentive_programs") or [])
        nxt = str(payload.get("next_cursor") or "")
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    return rows


def load_readonly_programs(reader) -> list[dict]:
    """Incentive frames only. Shard and close_time come from the market cache."""
    return _programs_from_incentive({"incentive_programs": _incentive_rows(reader)})


def market_cache_path() -> Path:
    raw = os.environ.get("LIP_MARKET_CACHE", "").strip()
    if raw:
        return Path(raw)
    return Path("/var/lib/lip-maker/market_meta.json")


def load_market_cache(path: Path | None = None) -> dict:
    dest = market_cache_path() if path is None else Path(path)
    try:
        payload = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_market_cache(cache: dict, path: Path | None = None) -> None:
    dest = market_cache_path() if path is None else Path(path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(dest)
    except OSError as exc:
        _log.warning("market cache not saved (%s)", exc)


def _parse_ts(text) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def horizon_close_ts(raw: dict) -> float | None:
    """Earlier of close_time and occurrence_datetime.

    A same-day match can list a close_time days out and an
    occurrence_datetime tonight. The short-close gate uses the earlier
    one. A missing side is ignored. Program end is not a fallback.
    """
    close = _parse_ts(
        raw.get("close_time") or raw.get("expected_expiration_time") or raw.get("expiration_time")
    )
    occurrence = _parse_ts(raw.get("occurrence_datetime"))
    stamps = [ts for ts in (close, occurrence) if ts is not None]
    if not stamps:
        return None
    return min(stamps)


def _parse_close_ts(raw: dict) -> float | None:
    return horizon_close_ts(raw)


def parse_market_row(raw: dict) -> dict | None:
    from mm.venues.kalshi import exchange_index_from_market_payload
    if not isinstance(raw, dict):
        return None
    ticker = str(raw.get("ticker") or raw.get("market_ticker") or "")
    if not ticker:
        return None
    idx = exchange_index_from_market_payload({"market": raw})
    return {
        "ticker": ticker,
        "exchange_index": None if idx is None else int(idx),
        "close_ts": _parse_close_ts(raw),
        "category": str(raw.get("category") or raw.get("event_category") or ""),
    }


def apply_market_meta(frame: dict, meta: dict | None, *, now: float | None = None) -> dict:
    """Horizon follows the market close, not the incentive program end."""
    out = dict(frame)
    if not meta:
        return out
    now = time.time() if now is None else float(now)
    if meta.get("exchange_index") is not None:
        out["exchange_index"] = int(meta["exchange_index"])
    if meta.get("category"):
        out["category"] = str(meta["category"])
    close = meta.get("close_ts")
    if close is not None:
        out["close_ts"] = float(close)
        out["days_to_settle"] = max(0.0, (float(close) - now) / 86400.0)
    return out


def _market_rows(payload: dict) -> list[dict]:
    if isinstance(payload.get("markets"), list):
        return [row for row in payload["markets"] if isinstance(row, dict)]
    if isinstance(payload.get("market"), dict):
        return [payload["market"]]
    return []


MARKET_BATCH_TICKERS = 100


def refresh_market_cache(reader, tickers: list[str], cache: dict | None = None) -> dict:
    """GET /markets?tickers=... in batches. Writes the on-disk cache."""
    from mm.venues.readonly import ReadOnlyDataError
    store = dict(cache or {})
    unique: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        key = str(ticker)
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    for start in range(0, len(unique), MARKET_BATCH_TICKERS):
        chunk = unique[start:start + MARKET_BATCH_TICKERS]
        _shards._acquire()
        try:
            payload = reader.get(
                "/markets", params={"tickers": ",".join(chunk), "limit": str(len(chunk))},
            )
        except ReadOnlyDataError as exc:
            _log.warning("read-only market batch failed (%s)", exc)
            continue
        for row in _market_rows(payload):
            parsed = parse_market_row(row)
            if parsed:
                store[parsed["ticker"]] = parsed
    save_market_cache(store)
    return store


def durable_frame_reason(frame: dict) -> str:
    days = frame.get("days_to_settle")
    market = KalshiMarket(
        market=str(frame.get("market") or ""),
        series=str(frame.get("series") or ""),
        period_reward_usd=float(frame.get("period_reward_usd") or 0),
        period_seconds=float(frame.get("period_seconds") or 86400),
        seconds_left=float(frame.get("period_seconds") or 0),
        discount_factor=float(frame.get("discount_factor") or 0.5),
        target_size=float(frame.get("target_size") or 1),
        days_to_settle=None if days is None else float(days),
        exchange_index=0,
        category=str(frame.get("category") or ""),
    )
    from mm.selector import exclusion_reason
    why = exclusion_reason(market)
    if why == "shard_unknown":
        return ""
    return why


def _frame_market(frame: dict) -> KalshiMarket:
    days = frame.get("days_to_settle")
    return KalshiMarket(
        market=str(frame.get("market") or ""),
        series=str(frame.get("series") or ""),
        period_reward_usd=float(frame.get("period_reward_usd") or 0),
        period_seconds=float(frame.get("period_seconds") or 86400),
        seconds_left=float(frame.get("period_seconds") or 86400),
        discount_factor=float(frame.get("discount_factor") or 0.5),
        target_size=float(frame.get("target_size") or CANDIDATE_SIZE),
        yes_bids=list(frame.get("yes_bids") or []),
        no_bids=list(frame.get("no_bids") or []),
        days_to_settle=None if days is None else float(days),
        exchange_index=0,
        category=str(frame.get("category") or ""),
    )


def candidate_tickers(frames: list[dict], *, limit: int | None = None) -> list[str]:
    """Durable names ranked by expected net $/day per $ of capital.

    The score is the share at ``CANDIDATE_SIZE`` minus the adverse-selection
    penalty (larger as days-to-close shrinks, and larger for news-driven
    categories). It is not the raw pool per day. The list is capped at
    ``LIP_CANDIDATE_TOP`` (default 1000).
    """
    cap = candidate_top() if limit is None else int(limit)
    ranked = []
    for frame in frames:
        if durable_frame_reason(frame):
            continue
        market = str(frame.get("market") or "")
        if not market:
            continue
        score = expected_net_per_dollar(_frame_market(frame), CANDIDATE_SIZE)
        ranked.append((score, market))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [market for _score, market in ranked[:cap]]


def _frames_with_meta(frames: list[dict], cache: dict, *, now: float | None = None) -> list[dict]:
    return [
        apply_market_meta(frame, cache.get(str(frame.get("market") or "")), now=now)
        for frame in frames
    ]


async def enrich_and_subscribe(sock, reader, frames, on_frame, *, channels) -> list[str]:
    """Subscribe from the cache immediately, and refresh shards in the background.

    The caller has already connected the websocket. This does not block that
    connection on the market lookup: a warm cache subscribes first, then the
    batch GET runs. A cold cache subscribes once the batch returns.
    """
    import asyncio
    cache = load_market_cache()
    primed = _frames_with_meta(frames, cache)
    subscribed: list[str] = []

    async def _subscribe(rows: list[dict]) -> None:
        nonlocal subscribed
        names = candidate_tickers(rows)
        if names == subscribed:
            return
        await sock.subscribe(list(channels), names)
        subscribed = list(names)

    if any(frame.get("days_to_settle") is not None for frame in primed):
        for frame in primed:
            on_frame(frame)
        await _subscribe(primed)
    tickers = [str(frame.get("market") or "") for frame in frames]
    refreshed = await asyncio.to_thread(refresh_market_cache, reader, tickers, cache)
    updated = _frames_with_meta(frames, refreshed)
    for frame in updated:
        on_frame(frame)
    await _subscribe(updated)
    return subscribed


async def drive_readonly_books(source: dict, on_frame: Callable[[dict], None]) -> None:
    """Production books and public trades. The reader cannot place an order.

    A missing HTTP session used to refuse the first GET with
    ``SystemExit(3)``, which systemd treated as a crash. The session is
    created here. An HTTP or network failure on an allowed GET logs,
    waits, and returns so the run loop can try again. A write, order, or
    portfolio refusal still ends the process.
    """
    import asyncio
    import requests
    from mm.venues.readonly import (
        PUBLIC_WS_CHANNELS, ReadOnlyKalshiTransport, ReadOnlyMarketSocket,
        ReadOnlyViolation, load_private_key,
    )
    key = load_private_key(source["key_path"])
    reader = ReadOnlyKalshiTransport(
        api_key=source["key_id"], private_key=key, session=requests.Session(),
    )
    frames: list[dict] = []
    try:
        frames = load_readonly_programs(reader)
    except ReadOnlyViolation:
        raise
    except Exception as exc:
        _log.warning(
            "read-only market data failed (%s); backing off %.0fs",
            exc, READONLY_DATA_BACKOFF_S,
        )
        await asyncio.sleep(READONLY_DATA_BACKOFF_S)
        return
    sock = ReadOnlyMarketSocket(api_key=source["key_id"], private_key=key, url=source["ws_url"])
    try:
        await sock.connect()

        async def _pump() -> None:
            async for raw in sock._ws:
                msg = json.loads(raw)
                msg.setdefault("ts", time.time())
                kind = str(msg.get("type") or "")
                if kind == "trade":
                    body = msg.get("msg") or msg
                    on_frame({"type": "trade", "ts": msg["ts"], "trade": body})
                elif kind in ("orderbook_snapshot", "orderbook_delta"):
                    on_frame(msg)

        clock = asyncio.create_task(_emit_clock(on_frame))
        try:
            await asyncio.gather(
                enrich_and_subscribe(
                    sock, reader, frames, on_frame, channels=sorted(PUBLIC_WS_CHANNELS),
                ),
                _pump(),
            )
        finally:
            clock.cancel()
            await clock
    except ReadOnlyViolation:
        raise
    except Exception as exc:
        _log.warning(
            "read-only market data failed (%s); backing off %.0fs",
            exc, READONLY_DATA_BACKOFF_S,
        )
        await asyncio.sleep(READONLY_DATA_BACKOFF_S)
    finally:
        await sock.close()


def _programs_from_incentive(payload: dict) -> list[dict]:
    from engine.lip_discovery import _parse_program
    frames = []
    for raw in payload.get("incentive_programs") or []:
        parsed = _parse_program(raw)
        if not parsed:
            continue
        start = datetime.fromisoformat(str(parsed["start_date"]).replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(str(parsed["end_date"]).replace("Z", "+00:00")).timestamp()
        frame = {
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
        }
        frames.append(frame)
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
