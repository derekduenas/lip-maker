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

Worse, `dislocation/spread.py:63-66` carries the comment

    fee = ceil(0.07 x C x P x (1-P)) cents per side

but the code applies no ceiling. A repo-wide search found the per-contract
round-up implemented nowhere. For the small per-fill contract counts a LIP
maker actually gets, the round-up is the DOMINANT term — a 1-contract fill at
50c is ~0.0175c of raw fee and 1c after rounding, a ~57x understatement. Every
edge estimate built on those constants was systematically optimistic.

Why this module refuses to be confident
---------------------------------------
The real Kalshi schedule is UNVERIFIED here: docs.kalshi.com is blocked by
this environment's egress proxy (403 to CONNECT), so the formula below could
not be checked against the current fee page. See
docs/CLAUDE_INDEPENDENT_ASSESSMENT.md §0.

So the schedule carries its own provenance and says out loud that it is
unverified. `verified` is False until someone with access confirms it and
records that in the constructor. Callers that must not guess can demand
`require_verified=True` and get an exception instead of a plausible number.

The default is deliberately CONSERVATIVE — it assumes we pay the documented
taker-style fee on both entry and exit unless told otherwise. Being wrong in
that direction understates profit; the $0.00 assumption it replaces was wrong
in the direction that manufactures it.
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

    def fee_usd(self, price_cents, contracts, *, is_taker: bool = False) -> Decimal:
        """Fee for one fill of `contracts` at `price_cents`.

        Documented-but-unverified Kalshi form:
            fee = ceil(rate x C x P x (1 - P)) cents
        where P is the price in dollars and C the contract count. The
        quadratic P(1-P) makes fees largest at 50c and vanish at the edges,
        which matches a risk-based charge on a binary.
        """
        if not is_taker and not self.charge_maker:
            return ZERO
        c = Decimal(str(contracts))
        if c <= 0:
            return ZERO
        p = Decimal(str(price_cents)) / CENTS
        if p <= 0 or p >= 1:
            return ZERO          # edge prices carry no risk premium
        raw_cents = self.rate * c * p * (Decimal(1) - p) * CENTS
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
                "charge_maker": self.charge_maker, "notes": self.notes}


# The working assumption. `verified=False` is load-bearing: it is what
# `require_verified=True` trips on, and what reports must surface.
KALSHI_UNVERIFIED = FeeSchedule(
    name="kalshi_documented_unverified",
    rate=Decimal("0.07"),
    source=("RATE unverified: kalshi.com/docs/kalshi-fee-schedule.pdf "
            "returned HTTP 429 on 2026-09-20. ROUNDING and maker-charging "
            "ARE verified — see docs/venue_evidence/kalshi_fees_20260920.json"),
    verified=False,
    rounding="ceil_6dp",
    charge_maker=True,
    notes=("Maker fills ARE charged (verified, help.kalshi.com): this "
           "refutes tools/net_yield_logger.py's uncited 'fee-free per "
           "Kalshi'. Rounding is ceil to $0.000001 (verified, "
           "docs.kalshi.com/getting_started/fee_rounding), NOT to the whole "
           "cent as this repo briefly implemented — that overstated the fee "
           "on small fills by orders of magnitude. The RATE constant 0.07 "
           "remains unverified, so verified=False stands."),
)

# An explicit zero, for A/B-ing the old assumption. Never the default.
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
