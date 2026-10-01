"""Rank incentive pools by net expected yield.

    NEY = (reward_share + maker_rebate − adverse_selection − fees − holding)
          / capital_at_risk

Reward share is the caller's estimate from the venue-correct scorer
(``engine.lip_scorer`` or ``polymarket.engine.pm_us_lip_scorer``). This
module does not reimplement scoring. It refuses long-dated event markets.
Markets with an external reference get a 0.01 NEY bonus — enough to win a
tie or a one-percent gap, not enough to quote a toxic book over a clean one.

Numbers in are Decimals where they are money. The markout is cents per
contract, negative when the fill is toxic.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from mm.fair_value import family_for_series
from mm.session_gates import close_horizon_reason, long_dated_event_days

ZERO = Decimal("0")
LONG_DATED_EVENT_DAYS = 95
LONG_DATED_ANY_DAYS = 120


@dataclass(frozen=True)
class Pool:
    market: str
    venue: str
    series: str
    days_to_settle: Optional[float]
    reward_share_usd: Decimal          # our expected LIP / incentive dollars
    expected_fills: Decimal            # contracts
    markout_cents: Decimal             # signed, per contract; negative = toxic
    fee_usd: Decimal                   # positive = we pay
    rebate_usd: Decimal                # positive = maker rebate we receive
    capital_usd: Decimal
    has_reference: bool = False
    has_observation: bool = False
    family: str = ""

    def __post_init__(self) -> None:
        if not self.family:
            object.__setattr__(self, "family", family_for_series(self.series))


@dataclass(frozen=True)
class Ranked:
    pool: Pool
    ney: Decimal
    excluded: bool
    reason: str


def _holding(pool: Pool) -> Decimal:
    """A flat penalty per day of inventory we cannot hedge, per contract
    of expected fills. Referenced markets pay a smaller rate because the
    external print is a way out; unreferenced events do not.
    """
    days = Decimal(str(pool.days_to_settle or 0))
    if days <= 1:
        return ZERO
    rate = Decimal("0.002") if pool.has_reference else Decimal("0.01")
    return rate * (days - 1) * pool.expected_fills


def net_dollars(pool: Pool) -> Decimal:
    as_cost = -(pool.markout_cents / Decimal(100)) * pool.expected_fills
    # markout is signed. Negative markout => positive cost. -(neg) = positive.
    # If markout is -2 cents, as_cost = -(-2/100)*fills = +0.02*fills. Yes.
    return (pool.reward_share_usd + pool.rebate_usd - as_cost - pool.fee_usd
            - _holding(pool))


def ney(pool: Pool) -> Decimal:
    if pool.capital_usd <= 0:
        return Decimal("-999")
    return net_dollars(pool) / pool.capital_usd


def exclusion_reason(pool: Pool) -> str:
    horizon = close_horizon_reason(pool.days_to_settle)
    if horizon:
        return horizon
    referenced = pool.family in ("commodity", "crypto") or (
        pool.family == "weather" and pool.has_observation)
    if pool.days_to_settle > long_dated_event_days() and not referenced:
        return f"long_dated_event_{pool.days_to_settle:.0f}d"
    if pool.capital_usd <= 0:
        return "no_capital"
    return ""


def rank_pools(pools: list[Pool]) -> list[Ranked]:
    """Eligible pools first, by (has a reference, NEY) descending.

    Excluded pools are returned after the eligible ones so a report can show
    why a famous market was skipped. They are not a quoting candidate.
    """
    ranked: list[Ranked] = []
    for p in pools:
        why = exclusion_reason(p)
        ranked.append(Ranked(p, ney(p), excluded=bool(why), reason=why or "ok"))
    ranked.sort(key=lambda r: (
        r.excluded,
        -(r.ney + (Decimal("0.01") if (
            not r.excluded and (r.pool.has_reference or r.pool.has_observation)
        ) else ZERO)),
    ))
    return ranked


def select(pools: list[Pool], *, max_markets: int = 10) -> list[Ranked]:
    out = []
    for row in rank_pools(pools):
        if row.excluded:
            break
        out.append(row)
        if len(out) >= max_markets:
            break
    return out
