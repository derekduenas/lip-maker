"""Compounding bankroll, shrink toward priors, and the scale ladder.

Realized rewards and fill P&L live on one ledger. Each day, capital moves
toward markets whose posterior net $/day per $ is highest. The posterior
shrinks the sample mean toward a prior. Size does not increase until the
sample count reaches the minimum.

A fractional-Kelly-style cap keeps any one market at or below
``fraction × equity``. Drawdown from the equity peak cuts every size in
half at 5% and flattens at 10%. The scale ladder is configuration:

    $500 → $1,000 → $2,500 → $5,000 → $10,000

A step requires ``n_days`` in a row with reward/markout at least 1.5 and
no kill. A miss resets that count. This module does not send orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LADDER_USD = (500, 1_000, 2_500, 5_000, 10_000)
PRIOR_STRENGTH = 5


@dataclass
class Posting:
    venue: str
    market: str
    series: str
    rewards_usd: float
    fill_pnl_usd: float


@dataclass
class BankrollLedger:
    opening_usd: float
    rows: list[Posting] = field(default_factory=list)

    def post(self, *, venue: str, market: str, series: str,
             rewards_usd: float = 0.0, fill_pnl_usd: float = 0.0) -> None:
        self.rows.append(Posting(venue, market, series, float(rewards_usd), float(fill_pnl_usd)))

    def net(self, *, venue: str | None = None, market: str | None = None,
            series: str | None = None) -> float:
        total = 0.0
        for row in self.rows:
            if venue is not None and row.venue != venue:
                continue
            if market is not None and row.market != market:
                continue
            if series is not None and row.series != series:
                continue
            total += row.rewards_usd + row.fill_pnl_usd
        return total

    def equity(self) -> float:
        return float(self.opening_usd) + self.net()


def observed_per_dollar(ledger: BankrollLedger, market: str, capital_usd: float) -> float:
    if capital_usd <= 0:
        return 0.0
    return ledger.net(market=market) / float(capital_usd)


def posterior(observed: float, prior: float, n: int, *,
              strength: int = PRIOR_STRENGTH) -> float:
    """Shrink the sample mean toward ``prior``. n = 0 returns the prior."""
    samples = max(0, int(n))
    k = max(0, int(strength))
    return (samples * float(observed) + k * float(prior)) / (samples + k)


@dataclass
class MarketSample:
    market: str
    observed_per_dollar: float
    prior_per_dollar: float
    n: int
    previous_usd: float
    venue: str = ""
    series: str = ""


def reallocate(samples: list[MarketSample], *, equity: float, peak: float,
               fraction: float = 0.25, min_sample: int = 5) -> dict[str, float]:
    """Dollars for the next day, keyed by market.

    Negative posterior weight gets nothing. A market with fewer than
    ``min_sample`` observations cannot grow past ``previous_usd``.
    """
    if peak <= 0 or equity < 0:
        return {sample.market: 0.0 for sample in samples}
    drawdown = (float(peak) - float(equity)) / float(peak)
    if drawdown >= 0.10 - 1e-12:
        return {sample.market: 0.0 for sample in samples}
    cut = 0.5 if drawdown >= 0.05 - 1e-12 else 1.0
    weighted = []
    for sample in samples:
        post = posterior(sample.observed_per_dollar, sample.prior_per_dollar, sample.n)
        weighted.append((sample, max(post, 0.0)))
    total = sum(weight for _sample, weight in weighted)
    budget = float(equity) * cut
    kelly_cap = float(fraction) * float(equity)
    out: dict[str, float] = {}
    for sample, weight in weighted:
        if total <= 0 or weight <= 0:
            out[sample.market] = 0.0
            continue
        proposed = min(budget * (weight / total), kelly_cap)
        if sample.n < min_sample:
            proposed = min(proposed, float(sample.previous_usd))
        out[sample.market] = proposed
    return out


class ScaleLadder:
    """One rung at a time. The good-day count resets after a step and after a miss."""

    def __init__(self, n_days: int = 5, rungs: tuple[float, ...] = LADDER_USD) -> None:
        if n_days < 1:
            raise ValueError("n_days must be >= 1")
        if not rungs:
            raise ValueError("ladder is empty")
        self.n_days = int(n_days)
        self.rungs = tuple(float(x) for x in rungs)
        self.step = 0
        self.good_days = 0

    @property
    def capital_usd(self) -> float:
        return self.rungs[self.step]

    def record_day(self, *, reward_usd: float, markout_cost_usd: float, kill: bool) -> None:
        if kill:
            self.good_days = 0
            return
        if markout_cost_usd <= 0:
            ok = reward_usd > 0
        else:
            ok = (reward_usd / markout_cost_usd) >= 1.5
        if not ok:
            self.good_days = 0
            return
        self.good_days += 1
        if self.good_days >= self.n_days and self.step < len(self.rungs) - 1:
            self.step += 1
            self.good_days = 0


def capped_equity(ledger: BankrollLedger, ladder: ScaleLadder) -> float:
    """The smaller of book equity and the rung the ladder currently allows."""
    return min(ledger.equity(), ladder.capital_usd)
