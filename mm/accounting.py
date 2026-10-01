"""Estimated rewards, paid rewards, and per-series fees stay in different columns.

An estimate is a model output. A paid figure requires a source from
``engine.reward_provenance.PAID_SOURCES``. Net cash uses paid rewards only.

Kalshi fees, fee schedule PDF "July 2026 — 7.7.26 Update"
(https://kalshi.com/docs/kalshi-fee-schedule.pdf, retrieved 2026-10-01):

    taker  = round_up(M × 0.07 × C × P × (1−P))
    maker  = round_up(M × 0.0175 × C × P × (1−P))   # only where maker fees apply

The Trade API series schema (fetched 2026-10-01) maps that onto ``fee_type``:

    quadratic                          no maker fee
    quadratic_with_maker_fees          maker coefficient 0.07 × 0.25 = 0.0175
    quadratic_with_combo_maker_fees    maker coefficient 0.07 × 0.50 = 0.035

``fee_multiplier`` on the series is M. Rounding in code stays the ceil-to
$0.000001 rule documented at docs.kalshi.com/getting_started/fee_rounding
(the PDF's "centicent" wording is not the rule the existing fee module
verified, so this module does not switch rounding).

PM US, https://docs.polymarket.us/fees fetched 2026-10-01, effective
00:00 ET on 25 September 2026:

    taker fee    0.0695 × C × p × (1−p)     banker's round to $0.01
    maker rebate 0.0125 × C × p × (1−p)     banker's round to $0.01, a credit
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional

from engine.fees import FeeSchedule
from engine.reward_provenance import PAID_SOURCES, PROV_ESTIMATE, PROV_NONE, PROV_PAID
from engine.series_fees import SeriesFeeSchedule, schedule_from_series

ZERO = Decimal("0")
CENT = Decimal("0.01")
TAKER = Decimal("0.07")
MAKER_STANDARD = Decimal("0.0175")   # 0.07 × 0.25
MAKER_COMBO = Decimal("0.035")       # 0.07 × 0.50
PM_TAKER = Decimal("0.0695")
PM_MAKER_REBATE = Decimal("0.0125")


def maker_coefficient(fee_type: str) -> Decimal:
    if fee_type == "quadratic":
        return ZERO
    if fee_type == "quadratic_with_maker_fees":
        return MAKER_STANDARD
    if fee_type == "quadratic_with_combo_maker_fees":
        return MAKER_COMBO
    raise ValueError(f"unknown fee_type {fee_type!r}")


def kalshi_fee_usd(price_cents: int, contracts, *, fee_type: str,
                   multiplier: Decimal = Decimal(1), is_taker: bool = False) -> Decimal:
    """One Kalshi fill. Unknown fee types raise; callers may fall back."""
    coef = TAKER if is_taker else maker_coefficient(fee_type)
    sched = FeeSchedule(
        name=f"kalshi[{fee_type}]",
        rate=coef * multiplier,
        source="kalshi fee schedule PDF 2026-07-07 + series fee_type",
        verified=False,
        rounding="ceil_6dp",
        charge_maker=coef > 0 or is_taker,
    )
    if not is_taker and coef == 0:
        return ZERO
    return sched.fee_usd(price_cents, contracts, is_taker=is_taker)


def pm_us_taker_fee_usd(price_cents: int, contracts) -> Decimal:
    return _pm(PM_TAKER, price_cents, contracts)


def pm_us_maker_rebate_usd(price_cents: int, contracts) -> Decimal:
    """Credit. Positive means the exchange pays us."""
    return _pm(PM_MAKER_REBATE, price_cents, contracts)


def _pm(theta: Decimal, price_cents: int, contracts) -> Decimal:
    c = Decimal(str(contracts))
    if c <= 0:
        return ZERO
    p = Decimal(int(price_cents)) / Decimal(100)
    if p <= 0 or p >= 1:
        return ZERO
    raw = theta * c * p * (Decimal(1) - p)
    return raw.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass
class RewardBook:
    """One market's rewards. Estimated and paid never share a field."""
    market: str
    estimated_usd: Decimal = ZERO
    paid_usd: Decimal = ZERO
    provenance: str = PROV_NONE
    source: str = ""
    fees_usd: Decimal = ZERO
    rebates_usd: Decimal = ZERO
    realized_usd: Decimal = ZERO

    def add_estimate(self, amount: Decimal) -> None:
        self.estimated_usd += amount
        if self.provenance != PROV_PAID:
            self.provenance = PROV_ESTIMATE

    def add_paid(self, amount: Decimal, source: str) -> None:
        if source not in PAID_SOURCES:
            raise ValueError(
                f"paid reward source {source!r} is not in {sorted(PAID_SOURCES)}")
        self.paid_usd += amount
        self.provenance = PROV_PAID
        self.source = source

    @property
    def cash_pnl_usd(self) -> Decimal:
        """Realized trading P&L + paid rewards + maker rebates − fees.

        Estimated rewards are not in this number.
        """
        return self.realized_usd + self.paid_usd + self.rebates_usd - self.fees_usd


@dataclass
class Books:
    rows: dict[str, RewardBook] = field(default_factory=dict)

    def book(self, market: str) -> RewardBook:
        if market not in self.rows:
            self.rows[market] = RewardBook(market)
        return self.rows[market]

    def cash_total(self) -> Decimal:
        return sum((b.cash_pnl_usd for b in self.rows.values()), ZERO)

    def estimated_total(self) -> Decimal:
        return sum((b.estimated_usd for b in self.rows.values()), ZERO)


def schedule_for(series_json: Optional[dict]) -> Optional[SeriesFeeSchedule]:
    if not series_json:
        return None
    sched = schedule_from_series(series_json)
    return sched
