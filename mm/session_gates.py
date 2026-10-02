"""Pre-trade gates: the pre-close quote pull, the single-fill size cap and
the live series gate, kept here so the quoter and the supervisor apply the
same rule.

Provenance: the gates were motivated by one paper session (1 October 2026)
in which quotes resting into the final hour before close took large
adverse fills. That session's figures were never saved as an artifact in
this repository and have not been reproduced, so none are quoted here. The
default constants (15-minute pull, $100 single-fill cap) are judgment
calls, not fitted values; tune them from recorded sessions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PULL_BEFORE_CLOSE_MIN = 15.0
DEFAULT_SINGLE_FILL_CAP_USD = 100.0


def pull_before_close_s(minutes: float | None = None) -> float:
    """Seconds before close_time at which quotes come off. Default 15 minutes."""
    if minutes is None:
        raw = os.environ.get("LIP_PULL_BEFORE_CLOSE_MIN", "")
        minutes = float(raw) if raw else DEFAULT_PULL_BEFORE_CLOSE_MIN
    return float(minutes) * 60.0


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
