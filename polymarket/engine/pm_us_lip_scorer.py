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

Rules re-checked against the docs on 2026-10-02 (FAQ at
docs.polymarket.us/incentives/liquidity, api-reference/incentives/overview,
polymarket.us/rewards). Former assumptions, now sourced:
  A1. Equal bid/ask weight: "the bid side and ask side are each
      independently normalized to 1.0 per snapshot, provided Target Size is
      met on that side", so a second's slice is split evenly between the
      two sides and a side short of Target pays nobody. Where the docs speak
      (Max Spread) a failed second "is forfeited, not shifted to other
      seconds or other makers"; the same no-redistribution is assumed for a
      lone unqualified side in programs without Max Spread (docs silent).
  A2. The straddling level scores whole: the walk goes "one whole price
      level at a time ... The price level that gets there is that side's
      size-adjusted price", and "every order from the best price through
      the size-adjusted price qualifies" (api-reference/incentives/overview).
  A3. A program window's rewardPool is "shared across the program's markets
      - never summed per market" (polymarket.us/rewards); with per-side
      normalization every member market carries an equal slice, so the pool
      is divided by the distinct active member markets carrying the same
      (programId, period) (``split_pool_usd``). Shared with
      mm/unattended/pmus_paper.py. Reconcile against GET
      /v1/incentives/earnings once a key exists.
Prices are handled in integer ticks to avoid float drift (tick 0.01 or
0.001 dollars).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
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
    # Gateway repeats rewardPool on every market in the program window.
    # n_markets is how many members share that one pool. 1 keeps a lone market.
    n_markets: int = 1


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


def effective_reward_pool_usd(reward_pool_usd: float, n_markets: int) -> float:
    """One program window has one pool. The API repeats it on every market.

    Divide by the member count: each member market's sides are normalized
    to 1.0 per qualifying snapshot, and an unqualified second is forfeited
    rather than redistributed (docs, Max Spread case; assumed otherwise), so
    every member carries an equal slice. ``n_markets`` below 1 is rejected.
    """
    n = int(n_markets)
    if n < 1:
        raise ValueError("n_markets must be >= 1")
    if reward_pool_usd <= 0:
        return 0.0
    return float(reward_pool_usd) / n


def expected_payout_usd(shares: Iterable[float], *, reward_pool_usd: float,
                        period_seconds: float, n_markets: int = 1) -> float:
    """Effective pool × (Σ per-second share) / seconds-in-period.

    Unobserved seconds must be passed as 0 — they still count toward the
    period. ``n_markets`` defaults to 1 so a single-market call is unchanged.
    ``payable`` is a separate $1 check whose unit (program-period versus
    user-per-day) is not verified against a payout statement, so callers
    that are estimating a slice should use this function and not payable().
    """
    effective = effective_reward_pool_usd(reward_pool_usd, n_markets)
    if period_seconds <= 0 or effective <= 0:
        return 0.0
    return effective * sum(shares) / period_seconds


def effective_pool_from_market(market: dict) -> float:
    """Prefer an explicit ``pool_eff``. Otherwise divide the repeated pool."""
    if market.get("pool_eff") is not None:
        return float(market["pool_eff"])
    raw = market.get("reward_pool_usd", market.get("rewardPool", 0))
    n = market.get("n_window", market.get("n_markets", 1))
    return effective_reward_pool_usd(float(raw or 0), int(n or 1))


# ---------------------------------------------------------------- pool split
# ONE rule for every PM US consumer (this module, mm/unattended/pmus_paper.py,
# mm/selector.pm_quote_economics): a program window's pool is keyed by
# (programId, period) and, by default, divided across the distinct member
# markets that carry that key with status "active".
#
# Why "members" is the default: the docs define rewardPool as the "Total
# reward pool for this period in USD" without saying whether it is per
# market; the changelog quotes budgets "per game" / "per event"; live, one
# programId repeats an identical pool across 9-41 (and up to thousands of)
# markets. Dividing is the conservative reading. It is an ASSUMPTION until
# it is reconciled against GET /v1/incentives/earnings (authenticated; not
# called by this repo). LIP_PMUS_POOL_SPLIT=market opts in to the optimistic
# whole-pool-per-market reading; any other value means "members".
POOL_SPLIT_ENV = "LIP_PMUS_POOL_SPLIT"
POOL_SPLIT_DEFAULT = "members"
POOL_SPLIT_MODES = ("members", "market")


def pool_split_mode(environ=None) -> str:
    """``members`` (default, conservative) or ``market`` (explicit opt-in)."""
    import os
    env = os.environ if environ is None else environ
    raw = str(env.get(POOL_SPLIT_ENV) or POOL_SPLIT_DEFAULT).strip().lower()
    return raw if raw in POOL_SPLIT_MODES else POOL_SPLIT_DEFAULT


def pool_key(program_id, period) -> tuple[str, str]:
    return (str(program_id or ""), str(period or ""))


def _is_active_liquidity(tp: dict) -> bool:
    return (tp.get("programType", "liquidityProgram") == "liquidityProgram"
            and str(tp.get("status", "")) == "active")


def count_pool_members(records: Iterable[dict]) -> Counter:
    """Distinct member markets per (programId, period) among active
    liquidityProgram periods of gateway /v1/incentives records."""
    seen: set = set()
    counts: Counter = Counter()
    for rec in records:
        slug = str(rec.get("marketSlug") or "")
        for tp in rec.get("timePeriods") or []:
            if not _is_active_liquidity(tp):
                continue
            key = pool_key(tp.get("programId"), tp.get("period"))
            if (key, slug) in seen:
                continue
            seen.add((key, slug))
            counts[key] += 1
    return counts


def split_pool_usd(reward_pool_usd: float, n_markets: int, mode: Optional[str] = None) -> float:
    """The pool one member market is credited with under ``mode``."""
    mode = pool_split_mode() if mode is None else mode
    if mode == "market":
        return float(reward_pool_usd) if reward_pool_usd > 0 else 0.0
    return effective_reward_pool_usd(reward_pool_usd, max(1, int(n_markets)))


def with_shared_pools(programs: list[PMProgram]) -> list[PMProgram]:
    """Mark every market with how many siblings share its program window
    (same keying as ``count_pool_members``: distinct active members per
    (program_id, period); a non-active program counts every sibling)."""
    active: dict = {}
    every: dict = {}
    for p in programs:
        key = pool_key(p.program_id, p.period)
        every.setdefault(key, set()).add(p.market_slug)
        if p.status == "active":
            active.setdefault(key, set()).add(p.market_slug)
    out = []
    for p in programs:
        key = pool_key(p.program_id, p.period)
        members = active.get(key) if p.status == "active" else every.get(key)
        out.append(replace(p, n_markets=max(1, len(members or ()))))
    return out


def payable(amount_usd: float) -> float:
    """Rewards under $1.00 are not paid out."""
    return amount_usd if amount_usd >= 1.0 else 0.0
