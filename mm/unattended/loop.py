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

_log = logging.getLogger("lip.readonly")
# Pause after a failed production-book read before the service tries again.
READONLY_DATA_BACKOFF_S = 5.0
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


class RunLoop:
    """One pass over recorded or live frames. The constructor opens nothing."""

    def __init__(self, *, mode: str = "paper", bankroll: float = 10_000.0,
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
        self.kill = None
        self.now = 0.0
        self.socket_opened = False

    def add_program(self, row: dict) -> None:
        market = str(row["market"])
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
        for market, accrual in self.accruals.items():
            open_s = self.open_seconds.get(market)
            if open_s is not None and open_s < second:
                accrual.score_second(open_s)
                self.open_seconds[market] = None
            accrual.omit_until(second)

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
        if not self.programs:
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
                days_to_settle=prog.days_to_settle,
                exchange_index=prog.exchange_index,
                shard_cash_usd=prog.shard_cash_usd,
            ))
        return rows

    def _select(self, ts: float) -> None:
        self.selection_count += 1
        self.last_select_ts = ts
        markets = self._markets()
        live = self.mode != "paper"
        selection = allocate(
            markets, bankroll=self.bankroll, chunk=self.chunk, max_size=self.chunk,
            per_market_usd=self.bankroll, per_series_usd=self.bankroll,
            per_category_usd=self.bankroll, live=live, series_stats=self.series_stats,
            single_fill_cap_usd=self.fill_cap,
        )
        self.excluded = list(selection.excluded)
        sized = optimize_sizes(
            markets, bankroll=self.bankroll, per_market_usd=self.bankroll,
            per_event_usd=self.bankroll, total_usd=self.bankroll,
            sizes=(self.chunk,), markout_usd_per_contract=0.0,
            single_fill_cap_usd=self.fill_cap,
        )
        chosen = {row.market: row for row in sized.chosen}
        taken = {row.market for row in selection.taken}
        for market in self.programs:
            row = chosen.get(market)
            if row is None or market not in taken or row.size <= 0:
                self._cancel(market, "not_selected")
                continue
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
            self.kill = {"reason": decision.reason, "cancel_all": decision.cancel_all,
                         "paper": self.mode == "paper"}
            self._cancel(market, decision.reason)
            if decision.cancel_all:
                self._cancel_all(decision.reason)
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
    payload: dict = {}
    try:
        payload = reader.get(
            "/incentive_programs", params={"status": "active", "type": "liquidity"},
        )
    except ReadOnlyViolation:
        raise
    except Exception as exc:
        _log.warning(
            "read-only market data failed (%s); backing off %.0fs",
            exc, READONLY_DATA_BACKOFF_S,
        )
        await asyncio.sleep(READONLY_DATA_BACKOFF_S)
        return
    for frame in _programs_from_incentive(payload):
        on_frame(frame)
    sock = ReadOnlyMarketSocket(api_key=source["key_id"], private_key=key, url=source["ws_url"])
    try:
        await sock.connect()
        tickers = [frame["market"] for frame in _programs_from_incentive(payload)]
        await sock.subscribe(sorted(PUBLIC_WS_CHANNELS), tickers)
        async for raw in sock._ws:
            msg = json.loads(raw)
            msg.setdefault("ts", time.time())
            kind = str(msg.get("type") or "")
            if kind == "trade":
                body = msg.get("msg") or msg
                on_frame({"type": "trade", "ts": msg["ts"], "trade": body})
            elif kind in ("orderbook_snapshot", "orderbook_delta"):
                on_frame(msg)
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
