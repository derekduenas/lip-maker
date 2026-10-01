"""Choose a size at the LIP reference price.

Objective at a candidate size:

    reward share × pool per day − expected markout

The share is the July 30 2026 snapshot share with our size joined at the
reference on each side. Markout is a positive dollar cost per contract
the caller supplies (a prior until fills exist).

Quiet multi-day programs are funded first when capital is scarce. A
program whose period is 15 minutes or less sits in ``short_pools`` and
is not sized unless ``enable_short_pools`` is set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mm.selector import (
    KalshiMarket, _reward_factor, competition_ratio, kalshi_share, reward_per_day,
)
from mm.session_gates import max_contracts_for_fill
from mm.unattended.feed import reference_cents

SHORT_POOL_SECONDS = 15 * 60
MULTIDAY_SECONDS = 2 * 86400


@dataclass
class Sized:
    market: str
    size: float
    yes_cents: int
    no_cents: int
    objective: float
    capital_usd: float
    share: float


@dataclass
class SizePlan:
    chosen: list[Sized] = field(default_factory=list)
    short_pools: list[str] = field(default_factory=list)
    objectives: dict[str, dict[float, float]] = field(default_factory=dict)


def _quiet_multiday(market: KalshiMarket) -> bool:
    return (market.period_seconds >= MULTIDAY_SECONDS
            and competition_ratio(market) < 1.0)


def _capital(yes_cents: int, no_cents: int, size: float) -> float:
    return (yes_cents / 100.0) * size + (no_cents / 100.0) * size


def _curve(market: KalshiMarket, sizes: tuple[float, ...],
           markout_usd_per_contract: float, *,
           reward_factor: float = 1.0) -> tuple[int, int, dict[float, float], float, float, float]:
    """Return reference prices, objective by size, and the best size's economics."""
    yes_cents = reference_cents(market.yes_bids, market.target_size)
    no_cents = reference_cents(market.no_bids, market.target_size)
    curve: dict[float, float] = {}
    if yes_cents is None or no_cents is None:
        return 0, 0, curve, 0.0, -1e18, 0.0
    best_size = 0.0
    best_obj = -1e18
    best_share = 0.0
    for size in sizes:
        share = kalshi_share(market, yes_cents, no_cents, float(size))
        reward = reward_per_day(share, market, reward_factor=reward_factor)
        cost = float(markout_usd_per_contract) * float(size) * 2.0
        objective = reward - cost
        curve[float(size)] = objective
        if objective > best_obj:
            best_size = float(size)
            best_obj = objective
            best_share = share
    if best_obj <= 0:
        return yes_cents, no_cents, curve, 0.0, best_obj, 0.0
    return yes_cents, no_cents, curve, best_size, best_obj, best_share


def optimize_sizes(markets: list[KalshiMarket], *, bankroll: float,
                   per_market_usd: float, per_event_usd: float, total_usd: float,
                   sizes: tuple[float, ...] = (10, 25, 50, 100),
                   markout_usd_per_contract: float = 0.0,
                   enable_short_pools: bool = False,
                   series_factors: dict[str, float] | None = None,
                   single_fill_cap_usd: float = 100.0) -> SizePlan:
    plan = SizePlan()
    eligible: list[KalshiMarket] = []
    for market in markets:
        if market.period_seconds <= SHORT_POOL_SECONDS and not enable_short_pools:
            plan.short_pools.append(market.market)
            continue
        eligible.append(market)

    scored = []
    for market in eligible:
        yes_c, no_c, curve, best_size, best_obj, best_share = _curve(
            market, sizes, markout_usd_per_contract,
            reward_factor=_reward_factor(market.series, series_factors))
        plan.objectives[market.market] = curve
        if best_size <= 0 or yes_c <= 0:
            continue
        capital = _capital(yes_c, no_c, best_size)
        scored.append((market, yes_c, no_c, best_size, best_obj, capital, best_share))

    scored.sort(key=lambda row: (
        0 if _quiet_multiday(row[0]) else 1,
        -row[4],
        row[0].market,
    ))
    spent_market: dict[str, float] = {}
    spent_event: dict[str, float] = {}
    spent_total = 0.0
    budget = min(float(bankroll), float(total_usd))
    for market, yes_c, no_c, size, objective, capital, share in scored:
        legal = min(float(size),
                    max_contracts_for_fill(yes_c, single_fill_cap_usd),
                    max_contracts_for_fill(no_c, single_fill_cap_usd))
        if legal <= 0:
            continue
        if legal + 1e-9 < float(size):
            size = legal
            capital = _capital(yes_c, no_c, size)
        event = market.series.upper()
        if capital > per_market_usd + 1e-9:
            continue
        if spent_event.get(event, 0.0) + capital > per_event_usd + 1e-9:
            continue
        if spent_total + capital > budget + 1e-9:
            continue
        spent_market[market.market] = capital
        spent_event[event] = spent_event.get(event, 0.0) + capital
        spent_total += capital
        plan.chosen.append(Sized(
            market=market.market, size=size, yes_cents=yes_c, no_cents=no_c,
            objective=objective, capital_usd=capital, share=share,
        ))
    return plan
