"""The one place this repository decides what a trade costs (2026-09-21).

What was wrong
--------------
Four incompatible fee models coexisted, and the most optimistic one fed the
headline metric:

    tools/net_yield_logger.py:109   fees = 0.0   "LIP maker orders are
                                    fee-free per Kalshi" — no source, and it
                                    feeds total_net_profit_usd
    research/maker_replay.py:20-21  $0.01 maker / $0.02 exit per contract,
                                    self-labelled "NOT the exchange schedule"
    dislocation/config.py:50        7% of value per fill
    engine/maker_rebate_scorer.py:44  0.07 — sourced to GEMINI's docs, not
                                    Kalshi, despite the identical number

(dislocation/ and engine/maker_rebate_scorer.py now live under
_archive/2026-10-01/; the line numbers refer to those archived copies.)

Worse, `dislocation/spread.py:63-66` (now _archive/2026-10-01/dislocation/)
carries the comment

    fee = ceil(0.07 x C x P x (1-P)) cents per side

but the code applies no ceiling. A repo-wide search found the per-contract
round-up implemented nowhere. For the small per-fill contract counts a LIP
maker actually gets, the round-up is the DOMINANT term — a 1-contract fill at
50c is ~0.0175c of raw fee and 1c after rounding, a ~57x understatement. Every
edge estimate built on those constants was systematically optimistic.

The schedule (Kalshi fee schedule PDF, effective 7 July 2026; quoted in
mm/accounting.py)
------------------------------------------------------------------------
    taker = round_up(M x 0.07   x C x P x (1-P))
    maker = round_up(M x 0.0175 x C x P x (1-P))  only on maker-fee series
                                                  (series fee_type
                                                  quadratic_with_maker_fees;
                                                  combo: 0.035), 0 otherwise

Per-series pricing lives in mm/accounting.kalshi_fee_usd and
engine/series_fees.py. This module's global default (KALSHI_DEFAULT) has no
series information, so it prices every market as a maker-fee series: maker
0.0175, taker 0.07 - the conservative case of the real schedule. It used to
charge the 0.07 TAKER rate on maker fills, 4x the documented maker fee.

`verified` stays False: the default does not know the series' fee_type, so
for most series (fee_type quadratic, no maker fee) it overstates the maker
fee. Callers that must not guess can demand `require_verified=True` and get
an exception instead of a plausible number.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

_log = logging.getLogger(__name__)

ZERO = Decimal("0")
CENTS = Decimal("100")


class UnverifiedFeeSchedule(RuntimeError):
    """A caller demanded a verified schedule and none has been confirmed."""


@dataclass(frozen=True)
class FeeSchedule:
    """A fee model plus where it came from.

    `rate` and `formula` describe the arithmetic; `source`, `verified` and
    `verified_at` describe how much you should trust it. A schedule with
    verified=False is a working assumption, not a fact.
    """
    name: str
    rate: Decimal
    source: str
    verified: bool = False
    verified_at: str = ""
    # How each fill's fee is rounded.
    #   "ceil_6dp"  — VERIFIED 2026-09-20 against
    #                 https://docs.kalshi.com/getting_started/fee_rounding:
    #                 "trade fee: ceil_6dp(model_fee) — rounded up to the
    #                 nearest $0.000001". This is the exchange's actual rule.
    #   "ceil_cent" — what this repo implemented on 2026-09-21 from a code
    #                 comment. It is WRONG and punitive: for the small fills
    #                 a LIP maker gets, rounding to a whole cent dominates
    #                 the fee (1 contract @5c: 0.3325c raw becomes 1c).
    #   "none"      — raw model fee, for A/B only.
    rounding: str = "ceil_6dp"
    # Retained so existing callers/tests that speak in cents keep working.
    # True is equivalent to rounding="ceil_cent".
    round_up_to_cent: bool = False
    # Whether resting (maker) fills are charged at all.
    # VERIFIED 2026-09-20 against help.kalshi.com/en/articles/13823805-fees:
    # "Maker fees are charged for orders placed that are not immediately
    # matched and are instead left as resting orders on the orderbook."
    # This refutes the repo's uncited "fee-free per Kalshi" assumption.
    charge_maker: bool = True
    notes: str = ""
    # Coefficient for maker (resting) fills when it differs from ``rate``
    # (Kalshi: 0.0175 maker vs 0.07 taker). None = ``rate`` for both.
    maker_rate: Optional[Decimal] = None

    def fee_usd(self, price_cents, contracts, *, is_taker: bool = False) -> Decimal:
        """Fee for one fill of `contracts` at `price_cents`.

        Kalshi form: fee = round_up(coef x C x P x (1 - P)), coef = ``rate``
        for a taker fill and ``maker_rate`` (else ``rate``) for a maker
        fill, P the price in dollars, C the contract count.
        """
        if not is_taker and not self.charge_maker:
            return ZERO
        c = Decimal(str(contracts))
        if c <= 0:
            return ZERO
        p = Decimal(str(price_cents)) / CENTS
        if p <= 0 or p >= 1:
            return ZERO          # edge prices carry no risk premium
        coef = self.rate if (is_taker or self.maker_rate is None) else self.maker_rate
        raw_cents = coef * c * p * (Decimal(1) - p) * CENTS
        mode = "ceil_cent" if self.round_up_to_cent else self.rounding
        if mode == "ceil_cent":
            cents = Decimal(math.ceil(raw_cents))
        elif mode == "ceil_6dp":
            # Ceil the DOLLAR fee to $0.000001, per the venue's rule.
            dollars = raw_cents / CENTS
            step = Decimal("0.000001")
            cents = (dollars / step).to_integral_value(
                rounding="ROUND_CEILING") * step * CENTS
        else:
            cents = raw_cents
        return cents / CENTS

    def round_trip_usd(self, entry_cents, exit_cents, contracts) -> Decimal:
        """Entry as maker plus exit as taker — the realistic path for
        inventory a maker has to unwind."""
        return (self.fee_usd(entry_cents, contracts, is_taker=False)
                + self.fee_usd(exit_cents, contracts, is_taker=True))

    def describe(self) -> dict:
        return {"name": self.name, "rate": str(self.rate), "source": self.source,
                "verified": self.verified, "verified_at": self.verified_at,
                "rounding": ("ceil_cent" if self.round_up_to_cent
                             else self.rounding),
                "round_up_to_cent": self.round_up_to_cent,
                "charge_maker": self.charge_maker,
                "maker_rate": None if self.maker_rate is None else str(self.maker_rate),
                "notes": self.notes}


# ── the venue's full fee pipeline ─────────────────────────────────────────
# VERIFIED 2026-09-20 against docs.kalshi.com/getting_started/fee_rounding.
# The trade fee is only the FIRST of four stages, and a blanket substitution
# of microdollars for cents models only that stage:
#
#   1. trade_fee     = ceil_6dp(model_fee)          -> $0.000001 granularity
#   2. aligned_change= floor_precision(revenue - trade_fee)
#                      precision: $0.01 non-direct members, $0.0001 direct
#   3. rounding_fee  = (revenue - trade_fee) - aligned_change
#   4. net_fee       = trade_fee + rounding_fee - rebate, floored at 0
#
# Stage 3 matters: flooring the BALANCE change to a cent means a non-direct
# member can pay up to ~1c more than the trade fee on a single fill. Stage 4
# is what stops that being a permanent overcharge — the accumulator carries
# the overpayment across an order's fills and rebates it in whole precision
# increments once enough has built up.

PRECISION_NON_DIRECT = Decimal("0.01")
PRECISION_DIRECT = Decimal("0.0001")
FEE_GRANULARITY = Decimal("0.000001")


def ceil_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding="ROUND_CEILING") * step


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding="ROUND_FLOOR") * step


@dataclass
class FillFeeResult:
    """One fill's fee, decomposed the way the venue computes it."""
    trade_fee_usd: Decimal
    rounding_fee_usd: Decimal
    rebate_usd: Decimal
    net_fee_usd: Decimal
    aligned_change_usd: Decimal
    precision: Decimal

    def describe(self) -> dict:
        return {"trade_fee_usd": str(self.trade_fee_usd),
                "rounding_fee_usd": str(self.rounding_fee_usd),
                "rebate_usd": str(self.rebate_usd),
                "net_fee_usd": str(self.net_fee_usd),
                "aligned_change_usd": str(self.aligned_change_usd),
                "precision": str(self.precision)}


class OrderFeeAccumulator:
    """Carries rounding overpayment across one order's fills.

    Without this, every fill of a multi-fill order would be charged its own
    sub-cent round-up and none of it returned — which overstates the cost of
    exactly the strategy that fills in small pieces.
    """

    def __init__(self, *, precision: Decimal = PRECISION_NON_DIRECT):
        self.precision = precision
        self.carried = ZERO          # overpaid rounding not yet rebated
        self.fills = 0

    def charge(self, *, revenue_usd, model_fee_usd) -> FillFeeResult:
        """Apply the pipeline to one fill.

        `revenue_usd` is the cash effect of the fill before fees, negative
        for a buy (we pay premium).
        """
        revenue = Decimal(str(revenue_usd))
        trade_fee = ceil_to(Decimal(str(model_fee_usd)), FEE_GRANULARITY)
        pre = revenue - trade_fee
        aligned = floor_to(pre, self.precision)
        rounding_fee = pre - aligned
        self.carried += rounding_fee
        # Rebate whole precision increments once enough has accumulated.
        rebate = floor_to(self.carried, self.precision)
        if rebate < ZERO:
            rebate = ZERO
        self.carried -= rebate
        net = trade_fee + rounding_fee - rebate
        if net < ZERO:
            net = ZERO
        self.fills += 1
        return FillFeeResult(trade_fee_usd=trade_fee,
                             rounding_fee_usd=rounding_fee,
                             rebate_usd=rebate, net_fee_usd=net,
                             aligned_change_usd=aligned,
                             precision=self.precision)


# A wide bracket for fee-dependent figures. Low end: the tiered maker rate
# reported by search summaries (5 basis points) - NOT verbatim from the
# schedule and possibly the perps table, so it is a bound, not a fact. High
# end: the 0.07 taker coefficient applied to every fill (above the 0.0175
# maker coefficient the default schedule charges).
RATE_RANGE_LOW = Decimal("0.0005")
RATE_RANGE_HIGH = Decimal("0.07")


def fee_range_usd(price_cents, contracts, *, is_taker: bool = False):
    """(low, high) for one fill while the rate is unverified."""
    s = active_schedule()
    low = FeeSchedule(name="range_low", rate=RATE_RANGE_LOW, source="bound",
                      rounding=s.rounding, charge_maker=s.charge_maker)
    high = FeeSchedule(name="range_high", rate=RATE_RANGE_HIGH, source="bound",
                       rounding=s.rounding, charge_maker=s.charge_maker)
    return (low.fee_usd(price_cents, contracts, is_taker=is_taker),
            high.fee_usd(price_cents, contracts, is_taker=is_taker))


# The global default when no series fee_type is known: the maker-fee-series
# case of the July 2026 schedule (maker 0.0175, taker 0.07). `verified=False`
# is load-bearing: it is what `require_verified=True` trips on, and it says
# the series' real fee_type was not applied (most series have no maker fee).
KALSHI_DEFAULT = FeeSchedule(
    name="kalshi_default_maker_fee_series",
    rate=Decimal("0.07"),
    maker_rate=Decimal("0.0175"),
    source=("kalshi.com/docs/kalshi-fee-schedule.pdf effective 2026-07-07: "
            "taker 0.07, maker 0.0175 on maker-fee series (0 otherwise); "
            "series fee_type unknown here, so the maker fee is assumed"),
    verified=False,
    rounding="ceil_6dp",
    charge_maker=True,
    notes=("Same rates as mm/accounting.kalshi_fee_usd for "
           "fee_type=quadratic_with_maker_fees. Use that (or "
           "engine.series_fees) when the series' fee_type is known: a "
           "'quadratic' series pays no maker fee. Rounding is ceil to "
           "$0.000001 (docs.kalshi.com/getting_started/fee_rounding)."),
)
# Old name kept for importers; it is the same schedule.
KALSHI_UNVERIFIED = KALSHI_DEFAULT

# An explicit zero maker fee (the 'quadratic' series case, or the old
# uncited assumption for A/B). Never the default.
ASSUME_FREE_MAKER = FeeSchedule(
    name="assume_free_maker",
    rate=Decimal("0.07"),
    source="legacy assumption from tools/net_yield_logger.py, uncited",
    verified=False,
    charge_maker=False,
    notes="Reproduces the pre-2026-09-21 behaviour. Use only to measure how "
          "much of a result depended on assuming zero fees.",
)

_ACTIVE: FeeSchedule = KALSHI_UNVERIFIED


def active_schedule() -> FeeSchedule:
    return _ACTIVE


def set_schedule(schedule: FeeSchedule) -> None:
    """Override the active schedule (tests, or a verified schedule once
    someone with docs access confirms one)."""
    global _ACTIVE
    _ACTIVE = schedule
    _log.info(f"fee schedule set to {schedule.name} "
              f"(verified={schedule.verified}, charge_maker={schedule.charge_maker})")


def fee_usd(price_cents, contracts, *, is_taker: bool = False,
            require_verified: bool = False) -> Decimal:
    """Fee for one fill under the active schedule.

    `require_verified=True` raises rather than returning a number that was
    never checked against the exchange. Use it anywhere a fee feeds a
    profitability claim."""
    s = active_schedule()
    if require_verified and not s.verified:
        raise UnverifiedFeeSchedule(
            f"fee schedule {s.name!r} is unverified ({s.source}); refusing to "
            f"supply a fee to a caller that requires a verified one")
    return s.fee_usd(price_cents, contracts, is_taker=is_taker)


def round_trip_usd(entry_cents, exit_cents, contracts, *,
                   require_verified: bool = False) -> Decimal:
    s = active_schedule()
    if require_verified and not s.verified:
        raise UnverifiedFeeSchedule(f"fee schedule {s.name!r} is unverified")
    return s.round_trip_usd(entry_cents, exit_cents, contracts)


def provenance_warning() -> Optional[str]:
    """One line for any report that includes a fee-dependent number."""
    s = active_schedule()
    if s.verified:
        return None
    return (f"Fees use UNVERIFIED schedule {s.name!r} ({s.source}). "
            f"Net figures depending on them are estimates, not measurements.")
