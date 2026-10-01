"""Pre-trade gates from the 1 October 2026 paper sim.

Durable-focus defaults (env-configurable): a market that closes in under
24 hours is out, live sports and esports matches are out, and the long-dated
window is 90 days for events and 120 days for anything. Those horizons are
the market ``close_time``, not the incentive program end. A plan richer than
$40/day per $100 of capital is flagged, not auto-traded past the other gates.


Three hours, 7,035 fills. Quoting through the last hour before close lost
about $924 a day per $1,000 of fills. Pulling quotes 15 minutes before
close flipped that to about +$238. Eleven fills lost more than $100 on
the premium of that one fill.

The close pull, the single-fill size, and the live series gate live here
so the quoter and the supervisor apply the same rule.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

DEFAULT_PULL_BEFORE_CLOSE_MIN = 15.0
DEFAULT_SINGLE_FILL_CAP_USD = 100.0
DEFAULT_MIN_HOURS_TO_CLOSE = 24.0
DEFAULT_LONG_DATED_EVENT_DAYS = 90.0
DEFAULT_LONG_DATED_ANY_DAYS = 120.0
DEFAULT_SUSPECT_USD_PER_100_DAY = 40.0
DEFAULT_SUBSCRIBE_LIMIT = 300
DEFAULT_MATCH_SERIES_DENY = r"(?i)(?:MATCH|GAME|FIGHT|BOUT)$"
DEFAULT_SPORTS_CATEGORIES = "sports,esports"


def pull_before_close_s(minutes: float | None = None) -> float:
    """Seconds before close_time at which quotes come off. Default 15 minutes."""
    if minutes is None:
        raw = os.environ.get("LIP_PULL_BEFORE_CLOSE_MIN", "")
        minutes = float(raw) if raw else DEFAULT_PULL_BEFORE_CLOSE_MIN
    return float(minutes) * 60.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return float(default)
    return float(raw)


def min_hours_to_close() -> float:
    """Drop a market whose close is closer than this. ``0`` disables the gate."""
    return _env_float("LIP_MIN_HOURS_TO_CLOSE", DEFAULT_MIN_HOURS_TO_CLOSE)


def long_dated_event_days() -> float:
    return _env_float("LIP_LONG_DATED_EVENT_DAYS", DEFAULT_LONG_DATED_EVENT_DAYS)


def long_dated_any_days() -> float:
    return _env_float("LIP_LONG_DATED_ANY_DAYS", DEFAULT_LONG_DATED_ANY_DAYS)


def suspect_usd_per_100_day() -> float:
    """Planned dollars per day per $100 of capital above which status says suspect."""
    return _env_float("LIP_SUSPECT_USD_PER_100_DAY", DEFAULT_SUSPECT_USD_PER_100_DAY)


def subscribe_limit() -> int:
    return max(1, int(_env_float("LIP_SUBSCRIBE_LIMIT", DEFAULT_SUBSCRIBE_LIMIT)))


def plan_per_hundred(planned_usd_per_day: float, capital_usd: float) -> float:
    capital = float(capital_usd)
    if capital <= 0:
        return 0.0
    return float(planned_usd_per_day) / capital * 100.0


def plan_is_suspect(planned_usd_per_day: float, capital_usd: float) -> bool:
    return plan_per_hundred(planned_usd_per_day, capital_usd) > suspect_usd_per_100_day()


def match_series_reason(series: str, market: str = "", category: str = "") -> str:
    """Live sports and esports matches. Category or a series-name pattern.

    ``LIP_MATCH_SERIES_DENY`` is a regex on the series ticker. Unset uses
    a suffix of MATCH, GAME, FIGHT, or BOUT. ``LIP_SPORTS_CATEGORIES`` is a
    comma list. Set either variable to ``-`` to turn that check off.
    """
    pattern = os.environ.get("LIP_MATCH_SERIES_DENY")
    if pattern is None:
        pattern = DEFAULT_MATCH_SERIES_DENY
    if pattern and pattern != "-":
        rx = re.compile(pattern)
        for raw in (series, market):
            head = (raw or "").upper().split("-", 1)[0]
            if head and rx.search(head):
                return "match_series"
    cats = os.environ.get("LIP_SPORTS_CATEGORIES")
    if cats is None:
        cats = DEFAULT_SPORTS_CATEGORIES
    banned = {part.strip().lower() for part in cats.split(",") if part.strip() and part.strip() != "-"}
    label = (category or "").strip().lower()
    if label and label in banned:
        return "sports_category"
    return ""


def close_horizon_reason(days_to_settle: float | None) -> str:
    """Short close and the any-market long-dated cap, from days until close."""
    if days_to_settle is None:
        return "settlement_time_unknown"
    days = float(days_to_settle)
    hours = days * 24.0
    minimum = min_hours_to_close()
    if minimum > 0 and hours < minimum:
        return "closes_within_24h"
    if days > long_dated_any_days():
        return f"long_dated_{days:.0f}d"
    return ""


def single_fill_cap_usd(cap: float | None = None) -> float:
    if cap is None:
        raw = os.environ.get("LIP_SINGLE_FILL_CAP_USD", "")
        cap = float(raw) if raw else DEFAULT_SINGLE_FILL_CAP_USD
    return float(cap)


def inside_close_window(close_ts: float, now: float, *,
                        pull_before_s: float | None = None) -> bool:
    """True from T-minus the pull window through close, inclusive of the boundary."""
    window = pull_before_close_s() if pull_before_s is None else float(pull_before_s)
    return float(now) >= float(close_ts) - window


def markets_to_cancel(closes: dict[str, float], now: float, *,
                      pull_before_s: float | None = None) -> list[str]:
    window = pull_before_close_s() if pull_before_s is None else float(pull_before_s)
    return sorted(
        market for market, close_ts in closes.items()
        if inside_close_window(float(close_ts), now, pull_before_s=window)
    )


def intraday_reason(series: str, market: str = "") -> str:
    """Hourly temperature (``KXTEMP...H``) and ``*15M`` series.

    Empty when the series is not one of those. The selector excludes them
    unless the caller explicitly allows intraday names.
    """
    names = []
    for raw in (series, market):
        head = (raw or "").upper().split("-", 1)[0]
        if head and head not in names:
            names.append(head)
    for name in names:
        if name.endswith("15M"):
            return "intraday_15m"
        if name.startswith("KXTEMP") and name.endswith("H"):
            return "intraday_hourly"
    return ""


def max_contracts_for_fill(price_cents: int, cap_usd: float | None = None) -> int:
    """Largest size whose premium, if that one fill settles at 0, is within the cap.

    A buy at ``price_cents`` can lose that many cents per contract. The
    default cap is $100.
    """
    price = int(price_cents)
    cap = single_fill_cap_usd(cap_usd)
    if price <= 0 or cap <= 0:
        return 0
    cap_cents = int(round(cap * 100.0))
    return cap_cents // price


def clamp_contracts(price_cents: int | None, size: float,
                    cap_usd: float | None = None) -> int:
    if price_cents is None:
        return max(0, int(size))
    return min(max(0, int(size)), max_contracts_for_fill(int(price_cents), cap_usd))


@dataclass(frozen=True)
class SeriesGateConfig:
    """What a series must show before the live selector may trade it."""
    min_days: float = 5.0
    min_settled_fills: int = 30
    reward_haircut: float = 0.5


@dataclass(frozen=True)
class SeriesStats:
    series: str
    days: float
    settled_fills: int
    net_usd: float
    reward_usd: float
    markout_5m_usd: float


def series_go(stats: SeriesStats | None, config: SeriesGateConfig | None = None
              ) -> tuple[bool, str]:
    """Go only when every live condition holds.

    * at least ``min_days`` of history
    * at least ``min_settled_fills`` settled fills
    * net P&L strictly positive
    * 5-minute markout cost per fill strictly under reward per fill
    * still strictly positive after a 50% reward haircut
      (``net - (1 - haircut) * reward``)
    """
    cfg = config or SeriesGateConfig()
    if stats is None:
        return False, "no_series_record"
    if float(stats.days) < float(cfg.min_days):
        return False, "days"
    if int(stats.settled_fills) < int(cfg.min_settled_fills):
        return False, "fills"
    if float(stats.net_usd) <= 0:
        return False, "net"
    fills = int(stats.settled_fills)
    if fills <= 0 or float(stats.reward_usd) <= 0:
        return False, "reward"
    reward_per = float(stats.reward_usd) / fills
    markout_per = float(stats.markout_5m_usd) / fills
    if not markout_per < reward_per:
        return False, "markout"
    haircut_net = float(stats.net_usd) - (1.0 - float(cfg.reward_haircut)) * float(stats.reward_usd)
    if haircut_net <= 0:
        return False, "haircut"
    return True, "go"
