"""Kalshi pool selector. Expected net dollars per day, per dollar of capital.

Scoring is ``engine.lip_scorer`` (30 July 2026 rules: reference at
target/5, discount factor per tick, a second pays nothing if either side
of the book is under target). The $1 market-period floor and the cent
floor are ``kalshi_period_payout``. A caller may pass a per-series
multiplier from matched paid/estimate ratios. The default multiplier is 1.
This module does not fit a factor to April or May 2026 payouts.

Polymarket US ranking waits. A caller that asks for it is told so and
is not scored with the repeated reward-pool figure.

Our size is merged into the book before the share is computed, and the
same size is passed as our quotes. The quote price is the LIP reference
(cumulative target/5) when that level exists, and the touch when the book
is still thinner than target/5. A larger size earns a smaller marginal
share because the book already contains everyone else.

Shard cash that cannot fund the quote excludes the market. Idle cash on
another shard is reported. Nothing here moves collateral.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from engine.lip_discovery import _parse_program
from engine.lip_scorer import (
    BookLevel, BookState, OurQuotes, ProgramParams, kalshi_period_payout,
    score_snapshot, snapshot_share,
)
from mm.accounting import kalshi_fee_usd
from mm.fair_value import family_for_series
from mm.session_gates import (
    SeriesGateConfig, SeriesStats, intraday_reason, max_contracts_for_fill,
    series_go,
)
from mm.unattended.feed import reference_cents

# Negative markout is toxic. Commodity weeklies were the books that paid.
# Long-dated events and adverse selection were the books that lost.
# These are priors, not a fit to any payout statement.
MARKOUT_PRIOR_CENTS = {
    "commodity": -0.15,
    "crypto": -0.80,
    "weather": -1.00,
    "event": -3.00,
}
FILL_FRACTION_PER_DAY = {
    "commodity": 0.02,
    "crypto": 0.05,
    "weather": 0.04,
    "event": 0.15,
}
# Durable-focus policy (2026-10-01). Env-configurable; read at call time.
#   LIP_LONG_DATED_EVENT_DAYS (90), LIP_LONG_DATED_ANY_DAYS (120): days to the
#     market's effective close (min of close_time and occurrence_datetime).
#   LIP_MIN_CLOSE_HOURS (24): exclude markets closing sooner than this.
#   LIP_SPORTS_DENYLIST (regex on series ticker), LIP_SPORTS_CATEGORIES
#     (comma list, from GET /series category), LIP_SPORTS_MAX_DAYS (14):
#     live/same-day sports and esports matches are excluded.
# The markout prior keeps its original 14-day long-dated penalty.
import os as _os
import re as _re

MARKOUT_LONG_DATED_DAYS = 14
# Single-game / single-match series. League futures (MVP, champion, season
# totals) are not denylisted; short-dated ones fall to the Sports category rule.
DEFAULT_SPORTS_DENYLIST = (
    r"(MATCH|GAME|MAP|FIGHT|BOUT|SPREAD|TOTALS?)$"
    r"|^KX(ATP|WTA|ITF|TT|LOL|CS2|CSGO|DOTA|VALORANT|ESPORT)"
)
DEFAULT_SPORTS_CATEGORIES = "Sports,Esports,eSports"


def _env_float(name: str, default: float) -> float:
    try:
        return float(_os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def long_dated_event_days() -> float:
    return _env_float("LIP_LONG_DATED_EVENT_DAYS", 90)


def long_dated_any_days() -> float:
    return _env_float("LIP_LONG_DATED_ANY_DAYS", 120)


def min_close_hours() -> float:
    if _os.environ.get("LIP_MIN_HOURS_TO_CLOSE"):
        return _env_float("LIP_MIN_HOURS_TO_CLOSE", 24)
    return _env_float("LIP_MIN_CLOSE_HOURS", 24)


def sports_max_days() -> float:
    return _env_float("LIP_SPORTS_MAX_DAYS", 14)


def sports_denylist() -> str:
    return _os.environ.get("LIP_SPORTS_DENYLIST") or DEFAULT_SPORTS_DENYLIST


def sports_categories() -> set[str]:
    raw = _os.environ.get("LIP_SPORTS_CATEGORIES") or DEFAULT_SPORTS_CATEGORIES
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


# Import-time values kept for callers that read the names.
LONG_DATED_EVENT_DAYS = long_dated_event_days()
LONG_DATED_ANY_DAYS = long_dated_any_days()
EMPIRICAL_BLEND = 0.7
EMPIRICAL_MIN_N = 5


@dataclass
class KalshiMarket:
    market: str
    series: str
    period_reward_usd: float
    period_seconds: float
    seconds_left: float
    discount_factor: float
    target_size: float
    yes_bids: list[tuple[int, float]] = field(default_factory=list)
    no_bids: list[tuple[int, float]] = field(default_factory=list)
    fee_type: str = "quadratic"
    days_to_settle: float | None = 1.0
    has_reference: bool = False
    has_observation: bool = False
    exchange_index: int | None = 0
    shard_cash_usd: float = 1e9
    other_shard_cash_usd: float = 0.0
    other_shard_index: int | None = None
    incumbent: bool = False
    entry_competition: float | None = None
    entry_markout_cents: float | None = None
    empirical_markout_cents: float | None = None
    empirical_n: int = 0
    category: str | None = None


@dataclass
class ShardMove:
    market: str
    from_shard: int | None
    to_shard: int
    idle_usd: float
    note: str


@dataclass
class Taken:
    market: str
    size: float
    marginal_net_per_day: float
    marginal_capital: float
    marginal_per_dollar: float
    net_per_day: float
    capital_usd: float
    share: float
    yes_cents: int
    no_cents: int


@dataclass
class Selection:
    taken: list[Taken] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    shard_moves: list[ShardMove] = field(default_factory=list)
    exits: list[tuple[str, str]] = field(default_factory=list)


def defer_polymarket(market: str) -> tuple[str, str]:
    """PM US selection is a later round. Do not score the repeated pool."""
    return market, "pm_us_deferred"


def _levels(pairs: list[tuple[int, float]]) -> list[BookLevel]:
    merged: dict[int, float] = {}
    for price, size in pairs:
        if size > 0:
            merged[int(price)] = merged.get(int(price), 0.0) + float(size)
    levels = [BookLevel(price, size) for price, size in merged.items()]
    levels.sort(key=lambda level: -level.price_cents)
    return levels


def _merge(levels: list[BookLevel], price: int, size: float) -> list[BookLevel]:
    out = [BookLevel(level.price_cents, level.size) for level in levels]
    found = False
    for level in out:
        if level.price_cents == price:
            level.size += size
            found = True
            break
    if not found and size > 0:
        out.append(BookLevel(int(price), float(size)))
    out.sort(key=lambda level: -level.price_cents)
    return out


def touch(bids: list[tuple[int, float]], default: int = 50) -> int:
    prices = [int(price) for price, size in bids if size > 0]
    return max(prices) if prices else default


def competition_ratio(market: KalshiMarket) -> float:
    if market.target_size <= 0:
        return 0.0
    yes = sum(size for _price, size in market.yes_bids)
    no = sum(size for _price, size in market.no_bids)
    return ((yes + no) / 2.0) / market.target_size


def family_of(market: KalshiMarket) -> str:
    return family_for_series(market.series)


def markout_cents(market: KalshiMarket) -> float:
    family = family_of(market)
    prior = MARKOUT_PRIOR_CENTS.get(family, MARKOUT_PRIOR_CENTS["event"])
    if (market.days_to_settle is not None and market.days_to_settle > MARKOUT_LONG_DATED_DAYS
            and family not in ("commodity", "crypto")):
        prior -= 1.0
    if (market.empirical_n >= EMPIRICAL_MIN_N
            and market.empirical_markout_cents is not None):
        return (EMPIRICAL_BLEND * market.empirical_markout_cents
                + (1.0 - EMPIRICAL_BLEND) * prior)
    return prior


def kalshi_share(market: KalshiMarket, yes_cents: int, no_cents: int,
                 size: float) -> float:
    yes_book = _merge(_levels(market.yes_bids), yes_cents, size)
    no_book = _merge(_levels(market.no_bids), no_cents, size)
    book = BookState(market_ticker=market.market, yes_bids=yes_book, no_bids=no_book)
    ours = OurQuotes(
        yes_bids=[BookLevel(yes_cents, size)],
        no_bids=[BookLevel(no_cents, size)],
    )
    params = ProgramParams(
        market_ticker=market.market,
        target_size=market.target_size,
        discount_factor=market.discount_factor,
        period_reward_usd=market.period_reward_usd,
        period_seconds=market.period_seconds,
    )
    return snapshot_share(score_snapshot(book, ours, params))


def kalshi_one_sided_share(market: KalshiMarket, side: str, price_cents: int,
                           size: float) -> float:
    """Snapshot share when we rest on one side only (the other side's book
    must still reach target for the snapshot to count)."""
    yes_book = _levels(market.yes_bids)
    no_book = _levels(market.no_bids)
    if side == "yes":
        yes_book = _merge(yes_book, price_cents, size)
        ours = OurQuotes(yes_bids=[BookLevel(price_cents, size)], no_bids=[])
    else:
        no_book = _merge(no_book, price_cents, size)
        ours = OurQuotes(yes_bids=[], no_bids=[BookLevel(price_cents, size)])
    book = BookState(market_ticker=market.market, yes_bids=yes_book, no_bids=no_book)
    params = ProgramParams(
        market_ticker=market.market, target_size=market.target_size,
        discount_factor=market.discount_factor, period_reward_usd=market.period_reward_usd,
        period_seconds=market.period_seconds,
    )
    return snapshot_share(score_snapshot(book, ours, params))


def _uptime(market: KalshiMarket) -> float:
    if market.period_seconds <= 0 or market.seconds_left <= 0:
        return 0.0
    return min(1.0, market.seconds_left / market.period_seconds)


def reward_per_day(share: float, market: KalshiMarket, *,
                   reward_factor: float = 1.0) -> float:
    """Period obligation under $1 pays nothing, including as a daily rate.

    ``reward_factor`` scales the payable estimate. 1 leaves the LIP formula
    unchanged. The factor is a calibrated multiplier, applied after the
    exchange floor.
    """
    uptime = _uptime(market)
    paid = kalshi_period_payout(share, market.period_reward_usd, uptime=uptime)
    days = (market.period_seconds / 86400.0) * uptime
    if paid <= 0 or days <= 0 or reward_factor <= 0:
        return 0.0
    return (paid / days) * float(reward_factor)


def _reward_factor(series: str, factors: dict[str, float] | None) -> float:
    if not factors:
        return 1.0
    if series in factors:
        return float(factors[series])
    return float(factors.get(series.upper(), 1.0))


def holding_model() -> str:
    """LIP_HOLDING_MODEL: 'legacy' (default; $0.01/contract/day to close) or 'carry'."""
    return (_os.environ.get("LIP_HOLDING_MODEL") or "legacy").strip().lower()


def carry_apr() -> float:
    try:
        return float(_os.environ.get("LIP_CARRY_APR", 0.10))
    except (TypeError, ValueError):
        return 0.10


def quote_economics(market: KalshiMarket, size: float, *,
                    reward_factor: float = 1.0) -> tuple[float, float, float, int, int]:
    """Return net $/day, capital, share, yes cents, no cents at ``size``."""
    yes_ref = reference_cents(market.yes_bids, market.target_size)
    no_ref = reference_cents(market.no_bids, market.target_size)
    # A book that already reaches target/5 is quoted at that reference.
    # A thinner book has no reference yet; the touch is the price that
    # can create one once our size is added.
    yes_cents = touch(market.yes_bids) if yes_ref is None else yes_ref
    no_cents = touch(market.no_bids) if no_ref is None else no_ref
    share = kalshi_share(market, yes_cents, no_cents, size) if size > 0 else 0.0
    reward = reward_per_day(share, market, reward_factor=reward_factor)
    family = family_of(market)
    fraction = FILL_FRACTION_PER_DAY.get(family, FILL_FRACTION_PER_DAY["event"])
    fills_side = size * fraction
    mo = markout_cents(market)
    as_cost = -(mo / 100.0) * (fills_side * 2.0)
    fee = float(kalshi_fee_usd(yes_cents, 1, fee_type=market.fee_type)) * fills_side
    fee += float(kalshi_fee_usd(no_cents, 1, fee_type=market.fee_type)) * fills_side
    days = market.days_to_settle or 0.0
    cheap = family in ("commodity", "crypto") or market.has_reference or (
        family == "weather" and market.has_observation)
    holding = 0.0
    if days > 1:
        if holding_model() == "carry":
            # Capital carry: each day's fills lock their premium until close,
            # charged at LIP_CARRY_APR. Markout/adverse selection is the
            # separate as_cost term (and the screen's days-shrinking penalty).
            locked = fills_side * (yes_cents + no_cents) / 100.0
            holding = carry_apr() / 365.0 * (days - 1.0) * locked
        else:
            rate = 0.002 if cheap else 0.01
            holding = rate * (days - 1.0) * (fills_side * 2.0)
    net = reward - as_cost - fee - holding
    capital = (yes_cents / 100.0) * size + (no_cents / 100.0) * size
    return net, capital, share, yes_cents, no_cents


def sports_reason(market: KalshiMarket) -> str:
    """Live/same-day sports and esports match markets."""
    series = (market.series or market.market.split("-", 1)[0]).upper()
    if _re.search(sports_denylist(), series):
        return "sports_match"
    category = (market.category or "").strip().lower()
    if (category and category in sports_categories()
            and market.days_to_settle is not None
            and market.days_to_settle <= sports_max_days()):
        return "sports_short_dated"
    return ""


def exclusion_reason(market: KalshiMarket, *, allow_intraday: bool = False) -> str:
    if not allow_intraday:
        short = intraday_reason(market.series, market.market)
        if short:
            return short
    if market.days_to_settle is None:
        return "settlement_time_unknown"
    if market.days_to_settle * 24.0 < min_close_hours():
        return f"closes_within_{min_close_hours():g}h"
    sport = sports_reason(market)
    if sport:
        return sport
    if market.days_to_settle > long_dated_any_days():
        return f"long_dated_{market.days_to_settle:.0f}d"
    family = family_of(market)
    referenced = family in ("commodity", "crypto") or (
        family == "weather" and market.has_observation)
    if market.days_to_settle > long_dated_event_days() and not referenced:
        return f"long_dated_event_{market.days_to_settle:.0f}d"
    if market.exchange_index is None:
        return "shard_unknown"
    return ""


def exit_reason(market: KalshiMarket) -> str:
    if (market.entry_competition is not None
            and competition_ratio(market) > market.entry_competition + 0.5):
        return "competition_spike"
    if market.entry_markout_cents is not None:
        if markout_cents(market) < market.entry_markout_cents - 0.5:
            return "toxicity"
    return ""


def rank_score(per_dollar: float, *, incumbent: bool, hysteresis: float) -> float:
    if incumbent:
        return per_dollar * (1.0 + hysteresis)
    return per_dollar


def _caps(bankroll: float, per_market: float | None, per_series: float | None,
          per_category: float | None) -> tuple[float, float, float]:
    if bankroll <= 1000:
        return (
            50.0 if per_market is None else per_market,
            100.0 if per_series is None else per_series,
            150.0 if per_category is None else per_category,
        )
    return (
        bankroll * 0.10 if per_market is None else per_market,
        bankroll * 0.20 if per_series is None else per_series,
        bankroll * 0.30 if per_category is None else per_category,
    )


def allocate(markets: list[KalshiMarket], *, bankroll: float, chunk: float = 10,
             max_size: float = 200, hysteresis: float = 0.15,
             per_market_usd: float | None = None,
             per_series_usd: float | None = None,
             per_category_usd: float | None = None,
             series_factors: dict[str, float] | None = None,
             allow_intraday: bool = False,
             live: bool = False,
             series_stats: dict[str, SeriesStats] | None = None,
             series_gate: SeriesGateConfig | None = None,
             single_fill_cap_usd: float = 100.0) -> Selection:
    """Greedy marginal net $/day per dollar, with caps and hysteresis.

    ``live=True`` trades a series only when ``series_go`` says so. Paper
    leaves that gate off. Hourly temperature and 15-minute names stay out
    unless ``allow_intraday`` is set. A chunk that would put one fill's
    premium over ``single_fill_cap_usd`` is not taken.
    """
    selection = Selection()
    cap_m, cap_s, cap_c = _caps(bankroll, per_market_usd, per_series_usd, per_category_usd)
    gate = series_gate or SeriesGateConfig()
    stats = series_stats or {}
    eligible: list[KalshiMarket] = []
    for market in markets:
        why = exclusion_reason(market, allow_intraday=allow_intraday)
        if why:
            selection.excluded.append((market.market, why))
            continue
        if live:
            record = stats.get(market.series.upper()) or stats.get(market.series)
            ok, gate_why = series_go(record, gate)
            if not ok:
                selection.excluded.append((market.market, f"series_gate:{gate_why}"))
                continue
        leaving = exit_reason(market)
        if leaving:
            selection.exits.append((market.market, leaving))
            continue
        eligible.append(market)

    size_of = {market.market: 0.0 for market in eligible}
    net_of = {market.market: 0.0 for market in eligible}
    capital_of = {market.market: 0.0 for market in eligible}
    series_cap: dict[str, float] = {}
    category_cap: dict[str, float] = {}
    cash = float(bankroll)

    while eligible and cash > 1e-9:
        best = None
        best_key = None
        for market in eligible:
            nxt = size_of[market.market] + chunk
            if nxt > max_size + 1e-9:
                continue
            factor = _reward_factor(market.series, series_factors)
            net, capital, share, yes_c, no_c = quote_economics(
                market, nxt, reward_factor=factor)
            if (nxt > max_contracts_for_fill(yes_c, single_fill_cap_usd)
                    or nxt > max_contracts_for_fill(no_c, single_fill_cap_usd)):
                continue
            d_net = net - net_of[market.market]
            d_cap = capital - capital_of[market.market]
            if d_cap <= 1e-12:
                continue
            if d_cap > cash + 1e-9:
                continue
            if capital > cap_m + 1e-9:
                continue
            series = market.series.upper()
            family = family_of(market)
            if series_cap.get(series, 0.0) + d_cap > cap_s + 1e-9:
                continue
            if category_cap.get(family, 0.0) + d_cap > cap_c + 1e-9:
                continue
            if capital > market.shard_cash_usd + 1e-9:
                if not any(name == market.market and why == "unfunded_shard"
                           for name, why in selection.excluded):
                    selection.excluded.append((market.market, "unfunded_shard"))
                if (market.other_shard_index is not None
                        and market.other_shard_cash_usd + 1e-9 >= capital
                        and not any(move.market == market.market
                                    for move in selection.shard_moves)):
                    selection.shard_moves.append(ShardMove(
                        market=market.market,
                        from_shard=market.other_shard_index,
                        to_shard=int(market.exchange_index),
                        idle_usd=market.other_shard_cash_usd,
                        note="idle cash on another shard would fund this quote; not transferred",
                    ))
                continue
            per = d_net / d_cap
            if per <= 0:
                continue
            score = rank_score(per, incumbent=market.incumbent, hysteresis=hysteresis)
            key = (score, per, market.market)
            if best_key is None or key > best_key:
                best_key = key
                best = (market, nxt, d_net, d_cap, per, net, capital, share, yes_c, no_c)
        if best is None:
            break
        (market, nxt, d_net, d_cap, per, net, capital, share, yes_c, no_c) = best
        size_of[market.market] = nxt
        net_of[market.market] = net
        capital_of[market.market] = capital
        series_cap[market.series.upper()] = series_cap.get(market.series.upper(), 0.0) + d_cap
        family = family_of(market)
        category_cap[family] = category_cap.get(family, 0.0) + d_cap
        cash -= d_cap
        selection.taken.append(Taken(
            market=market.market, size=nxt,
            marginal_net_per_day=d_net, marginal_capital=d_cap,
            marginal_per_dollar=per, net_per_day=net, capital_usd=capital,
            share=share, yes_cents=yes_c, no_cents=no_c,
        ))
    return selection


def fast_allocate(markets: list[KalshiMarket], *, per_market_usd: float, chunk: float = 100.0,
                  single_fill_cap_usd: float = 100.0,
                  allow_intraday: bool = False) -> Selection:
    """Paper-loop eligibility + economics at one size, one evaluation per market.

    Same exclusions, exits, fill-cap, per-market and shard checks as
    ``allocate`` with ``chunk == max_size`` and a non-binding cash pool. The
    per-series cap is not applied here; the loop's budget pass enforces it.
    """
    selection = Selection()
    for market in markets:
        why = exclusion_reason(market, allow_intraday=allow_intraday)
        if why:
            selection.excluded.append((market.market, why))
            continue
        leaving = exit_reason(market)
        if leaving:
            selection.exits.append((market.market, leaving))
            continue
        net, capital, share, yes_c, no_c = quote_economics(market, chunk)
        if (chunk > max_contracts_for_fill(yes_c, single_fill_cap_usd)
                or chunk > max_contracts_for_fill(no_c, single_fill_cap_usd)):
            continue
        if capital <= 1e-12 or capital > per_market_usd + 1e-9:
            continue
        if capital > market.shard_cash_usd + 1e-9:
            selection.excluded.append((market.market, "unfunded_shard"))
            continue
        per = net / capital
        if per <= 0:
            continue
        selection.taken.append(Taken(
            market=market.market, size=chunk, marginal_net_per_day=net,
            marginal_capital=capital, marginal_per_dollar=per, net_per_day=net,
            capital_usd=capital, share=share, yes_cents=yes_c, no_cents=no_c,
        ))
    return selection


class IncentivePoller:
    """Ingest already-fetched Kalshi incentive rows. No network."""

    def __init__(self, interval_sec: float = 180) -> None:
        self.interval_sec = float(interval_sec)
        self.last_fetch: float | None = None
        self.programs: list[dict] = []

    def due(self, now: float) -> bool:
        if self.last_fetch is None:
            return True
        return float(now) - self.last_fetch >= self.interval_sec

    def ingest(self, raw_programs: list[dict], now: float) -> list[dict]:
        if not self.due(now):
            return list(self.programs)
        parsed = []
        for raw in raw_programs:
            row = _parse_program(raw)
            if row and not row.get("paid_out"):
                parsed.append(row)
        self.programs = parsed
        self.last_fetch = float(now)
        return list(parsed)


def market_from_program(parsed: dict, **overrides) -> KalshiMarket:
    base = dict(
        market=parsed["market_ticker"],
        series=parsed.get("series_ticker") or parsed["market_ticker"].split("-", 1)[0],
        period_reward_usd=float(parsed["period_reward_usd"]),
        period_seconds=float(parsed["period_seconds"]),
        seconds_left=float(parsed["period_seconds"]),
        discount_factor=float(parsed["discount_factor"]),
        target_size=float(parsed["target_size"]),
    )
    base.update(overrides)
    return KalshiMarket(**base)


def backtest_kalshi(path: str, params_by_market: dict[str, KalshiMarket]) -> dict[str, float]:
    """Credit July-30 rewards from recorded books and our quotes.

    Raw accrual is summed across the file. The $1 floor and the cent floor
    run once per market at the end. Fills are not invented here.
    """
    from mm.recorder import read_records

    ours: dict[str, dict[str, tuple[int, float]]] = {}
    last_ts: dict[str, float] = {}
    raw: dict[str, float] = {}
    for rec in read_records(path):
        kind = rec.get("kind")
        if kind == "quote":
            market = rec["market"]
            ours.setdefault(market, {})[rec["side"]] = (
                int(rec["price_cents"]), float(rec["size"]))
        elif kind == "book":
            market = rec["market"]
            ts = float(rec["ts"])
            quote = ours.get(market) or {}
            prev = last_ts.get(market)
            last_ts[market] = ts
            params = params_by_market.get(market)
            if prev is None or params is None or "yes" not in quote or "no" not in quote:
                continue
            dt = ts - prev
            if dt <= 0:
                continue
            yes_c, yes_sz = quote["yes"]
            no_c, no_sz = quote["no"]
            size = min(yes_sz, no_sz)
            bookish = KalshiMarket(
                market=market,
                series=params.series,
                period_reward_usd=params.period_reward_usd,
                period_seconds=params.period_seconds,
                seconds_left=params.seconds_left,
                discount_factor=params.discount_factor,
                target_size=params.target_size,
                yes_bids=[(int(rec.get("yes_bid", yes_c)), float(rec.get("yes_size") or 0))],
                no_bids=[(int(rec.get("no_bid", no_c)), float(rec.get("no_size") or 0))],
                days_to_settle=params.days_to_settle,
            )
            share = kalshi_share(bookish, yes_c, no_c, size)
            rate = params.period_reward_usd / params.period_seconds
            raw[market] = raw.get(market, 0.0) + share * rate * dt
    return {market: kalshi_period_payout(1.0, accrued) for market, accrued in raw.items()}


def render_report(selection: Selection) -> str:
    lines = ["market\tsize\tnet_per_day\tper_dollar\tcapital\tshare"]
    final: dict[str, Taken] = {}
    for row in selection.taken:
        final[row.market] = row
    for row in final.values():
        per = row.net_per_day / row.capital_usd if row.capital_usd else 0.0
        lines.append(
            f"{row.market}\t{row.size:.0f}\t{row.net_per_day:.4f}\t"
            f"{per:.6f}\t{row.capital_usd:.2f}\t{row.share:.4f}"
        )
    if selection.excluded:
        lines.append("excluded")
        for market, reason in selection.excluded:
            lines.append(f"{market}\t{reason}")
    if selection.exits:
        lines.append("exits")
        for market, reason in selection.exits:
            lines.append(f"{market}\t{reason}")
    if selection.shard_moves:
        lines.append("shard_moves")
        for move in selection.shard_moves:
            lines.append(
                f"{move.market}\tshard {move.from_shard} -> {move.to_shard}\t"
                f"{move.idle_usd:.2f}\t{move.note}"
            )
    if not final and not selection.excluded and not selection.exits:
        lines.append("(no allocation)")
    return "\n".join(lines) + "\n"


def demo_selection() -> Selection:
    """One funded commodity weekly against an empty book. No network."""
    market = KalshiMarket(
        market="KXBRENT-26OCT07",
        series="KXBRENT",
        period_reward_usd=50.0,
        period_seconds=86400.0,
        seconds_left=86400.0,
        discount_factor=0.5,
        target_size=100.0,
        days_to_settle=3,
        exchange_index=2,
        shard_cash_usd=500.0,
    )
    return allocate([market], bankroll=500, chunk=100, max_size=100,
                    per_market_usd=100, per_series_usd=200, per_category_usd=300)


@dataclass
class PMQuote:
    """One PM US market scored with the shared program pool.

    ``reward_pool_usd`` is the figure the gateway repeats. ``n_markets`` is
    the member count that figure is divided by. Fills are expected contracts
    per day at ``fill_price_cents``; the rebate is rounded per fill.
    """
    slug: str
    reward_pool_usd: float
    n_markets: int
    period_seconds: float
    discount_factor: float = 0.5
    target_size: float = 100.0
    tick: float = 0.01
    our_bid: float = 0.49
    our_ask: float = 0.51
    our_size: float = 100.0
    markout_usd_per_day: float = 0.0
    fill_contracts: float = 0.0
    fill_price_cents: int = 50
    capital_usd: float = 100.0
    max_spread_usd: float | None = None
    competing_bids: list[tuple[float, float]] = field(default_factory=list)
    competing_asks: list[tuple[float, float]] = field(default_factory=list)


def pm_quote_economics(quote: PMQuote) -> tuple[float, float, float, float, float]:
    """Return net $/day, capital, $/day per $, reward $/day, rebate $/day.

    Presence is the whole period at the current snapshot share. The pool
    is the effective (shared) pool. ``payable`` is not applied: the $1
    unit on PM US is not verified.
    """
    from polymarket.engine.pm_us_lip_scorer import (
        Order, effective_reward_pool_usd, score_snapshot,
    )
    from mm.accounting import pm_us_maker_rebate_usd

    bids = [Order(price, size, ours=False) for price, size in quote.competing_bids]
    asks = [Order(price, size, ours=False) for price, size in quote.competing_asks]
    bids.append(Order(quote.our_bid, quote.our_size, ours=True))
    asks.append(Order(quote.our_ask, quote.our_size, ours=True))
    snap = score_snapshot(
        bids, asks, tick=quote.tick, discount_factor=quote.discount_factor,
        target_size=quote.target_size, max_spread_usd=quote.max_spread_usd,
    )
    effective = effective_reward_pool_usd(quote.reward_pool_usd, quote.n_markets)
    days = quote.period_seconds / 86400.0
    reward = (effective * snap.our_share / days) if days > 0 else 0.0
    rebate = float(pm_us_maker_rebate_usd(quote.fill_price_cents, quote.fill_contracts))
    net = reward + rebate - float(quote.markout_usd_per_day)
    capital = float(quote.capital_usd)
    per = net / capital if capital > 0 else 0.0
    return net, capital, per, reward, rebate


def rank_cross_venue(kalshi: list[KalshiMarket], pm: list[PMQuote], *,
                     kalshi_size: float = 100.0,
                     series_factors: dict[str, float] | None = None) -> list[tuple[str, str, float]]:
    """Kalshi and PM US together, best net $/day per $ first.

    Kalshi uses ``quote_economics``. PM uses the shared pool and the
    per-fill maker rebate. Fees on Kalshi come from the series fee type
    inside ``quote_economics``.
    """
    rows: list[tuple[str, str, float]] = []
    for market in kalshi:
        factor = _reward_factor(market.series, series_factors)
        net, capital, _share, _yes, _no = quote_economics(
            market, kalshi_size, reward_factor=factor)
        per = net / capital if capital else -999.0
        rows.append(("kalshi", market.market, per))
    for quote in pm:
        _net, _capital, per, _reward, _rebate = pm_quote_economics(quote)
        rows.append(("pmus", quote.slug, per))
    rows.sort(key=lambda row: -row[2])
    return rows
