"""Polymarket US Liquidity Incentive Program scorer (2026-09-30).

Why a new module
----------------
polymarket/engine/pm_scorer.py and rewards_schedule.py implement the
Polymarket INTERNATIONAL (CLOB) rewards formula — quadratic spread score
S=((v-s)/v)^2·b, a Q_min two-sided gate with c=3, per-minute sampling and
weekly epochs. That is not how Polymarket US pays. Its program (docs
retrieved 2026-09-30, https://docs.polymarket.us/incentives/liquidity.md):

  * a random snapshot every second;
  * per order: Score = DiscountFactor ^ (ticks from best price) × size;
  * walk each side from the best price outward until cumulative RAW size
    reaches Target Size; orders inside that walk score, deeper ones do not;
    a side that never reaches Target Size does not qualify;
  * each side is normalized to 1.0 per qualifying snapshot;
  * optional Max Spread: both sides must reach Target Size and each side's
    size-adjusted price (where its walk lands) must be within Max Spread of
    the midpoint of the two size-adjusted prices, else NOBODY is paid that
    second (forfeited, not redistributed);
  * without Max Spread, each side is scored on its own and a one-sided
    quote still earns on the side it rests on;
  * pools are per time period (early/pre-game, day-of, live, daily); no
    rewards for cancelled/postponed games; payouts under $1 are not paid.

Live parameters come from the public gateway
GET https://gateway.polymarket.us/v1/incentives (no key) — the repo's
README claim that PM US has "no incentives API" is out of date.

Stated assumptions (not in the docs, flagged so they can be checked
against a real payout statement):
  A1. The per-second pool slice is split equally between the bid side and
      the ask side; a side that does not qualify forfeits its half.
  A2. At the level where the walk reaches Target Size, every order at that
      price level scores (the docs say "orders within that range score").
Prices are handled in integer ticks to avoid float drift (tick 0.01 or
0.001 dollars).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class PMProgram:
    market_slug: str
    program_id: str
    period: str
    reward_pool_usd: float
    discount_factor: float
    target_size: float
    max_spread_usd: Optional[float]
    start: Optional[str]
    status: str


def parse_incentives(record: dict) -> list[PMProgram]:
    """Parse one gateway /v1/incentives market record into programs."""
    out = []
    slug = record.get("marketSlug", "")
    for tp in record.get("timePeriods") or []:
        if tp.get("programType", "liquidityProgram") != "liquidityProgram":
            continue
        try:
            out.append(PMProgram(
                market_slug=slug,
                program_id=str(tp.get("programId", "")),
                period=str(tp.get("period", "")),
                reward_pool_usd=float(tp["rewardPool"]),
                discount_factor=float(tp["discountFactor"]),
                target_size=float(tp["targetSize"]),
                max_spread_usd=(float(tp["maxSpread"]) if tp.get("maxSpread") is not None else None),
                start=tp.get("start"),
                status=str(tp.get("status", "")),
            ))
        except (KeyError, TypeError, ValueError):
            continue          # malformed period: skip, never guess
    return out


@dataclass(frozen=True)
class Order:
    price: float      # dollars
    size: float
    ours: bool = False


@dataclass
class SideResult:
    qualified: bool
    ours: float = 0.0
    total: float = 0.0
    size_adjusted_price: Optional[float] = None

    @property
    def share(self) -> float:
        return self.ours / self.total if self.qualified and self.total > 0 else 0.0


def _ticks(price: float, tick: float) -> int:
    return int(round(price / tick))


def score_side(orders: Sequence[Order], *, is_bid: bool, tick: float,
               discount_factor: float, target_size: float) -> SideResult:
    if not orders:
        return SideResult(qualified=False)
    # Group by price level, best first.
    levels: dict[int, list[Order]] = {}
    for o in orders:
        if o.size > 0:
            levels.setdefault(_ticks(o.price, tick), []).append(o)
    if not levels:
        return SideResult(qualified=False)
    keys = sorted(levels, reverse=is_bid)
    best = keys[0]
    cum = 0.0
    ours = total = 0.0
    sap = None
    for k in keys:
        dist = abs(k - best)
        w = discount_factor ** dist
        for o in levels[k]:
            total += w * o.size
            if o.ours:
                ours += w * o.size
            cum += o.size
        if cum >= target_size:
            sap = k * tick
            break
    if sap is None:
        return SideResult(qualified=False)
    return SideResult(qualified=True, ours=ours, total=total, size_adjusted_price=sap)


@dataclass
class SnapshotResult:
    paid: bool
    bid: SideResult
    ask: SideResult
    reason: str = ""

    @property
    def our_share(self) -> float:
        """Our fraction of this second's pool slice (assumption A1)."""
        if not self.paid:
            return 0.0
        return 0.5 * self.bid.share + 0.5 * self.ask.share


def score_snapshot(bids: Sequence[Order], asks: Sequence[Order], *, tick: float,
                   discount_factor: float, target_size: float,
                   max_spread_usd: Optional[float] = None) -> SnapshotResult:
    b = score_side(bids, is_bid=True, tick=tick, discount_factor=discount_factor,
                   target_size=target_size)
    a = score_side(asks, is_bid=False, tick=tick, discount_factor=discount_factor,
                   target_size=target_size)
    if max_spread_usd is not None:
        if not (b.qualified and a.qualified):
            return SnapshotResult(False, b, a, "max_spread:side_short_of_target")
        mid = (b.size_adjusted_price + a.size_adjusted_price) / 2.0
        eps = tick / 1000.0
        if (mid - b.size_adjusted_price > max_spread_usd + eps
                or a.size_adjusted_price - mid > max_spread_usd + eps):
            return SnapshotResult(False, b, a, "max_spread:too_wide")
        return SnapshotResult(True, b, a)
    if not (b.qualified or a.qualified):
        return SnapshotResult(False, b, a, "no_side_qualified")
    return SnapshotResult(True, b, a)


def expected_payout_usd(shares: Iterable[float], *, reward_pool_usd: float,
                        period_seconds: float) -> float:
    """Pool × (Σ per-second share) / seconds-in-period. Unobserved seconds
    must be passed as 0 — they still count toward the period."""
    if period_seconds <= 0 or reward_pool_usd <= 0:
        return 0.0
    return reward_pool_usd * sum(shares) / period_seconds


def payable(amount_usd: float) -> float:
    """Rewards under $1.00 are not paid out."""
    return amount_usd if amount_usd >= 1.0 else 0.0
