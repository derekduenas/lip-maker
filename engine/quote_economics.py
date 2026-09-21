"""Expected net economics of a quote, and the choice between quotes.

Why this exists
---------------
The operating loop selected quotes by reward capacity: size to the program's
target, rank by reward share or by capital. Reward share is not profit. A
market can pay the largest rebate in the book and still lose money once the
fill you accept to earn it, the fee you pay on it, and the cost of unwinding
the inventory it leaves are counted.

This module answers one question for one candidate quote:

    over a stated horizon, what is the expected NET dollar result?

        expected reward
      + expected trading P&L        (negative: adverse selection)
      - fees
      - inventory / exit costs
      - operating costs
      - uncertainty allowance
      ------------------------------
      = expected net

and then picks the best candidate including the always-available option of
NOT QUOTING, whose net is exactly zero. A candidate must beat doing nothing,
not merely be the least bad way of quoting.

Reuse, not a second strategy
----------------------------
The reward and adverse-selection terms come from cross_venue.yield_equation
(MarketYield), which the research code already uses — not from a parallel
implementation that could drift from it. Fees come from engine.fees. Exit
costs come from the same engine.inventory_exit policy the runner enforces.

Double-counting
---------------
MarketYield.expected_daily_rebate is ALREADY net of adverse_cost_per_day.
This module therefore takes the GROSS reward and the adverse cost as two
separate, legible terms and subtracts the adverse cost exactly once. There
is no second adverse-selection term anywhere: the trading-P&L line IS the
adverse-selection estimate.

Our own footprint
-----------------
MarketYield.our_share and .qualify_prob both put our_size into the aggregate
depth, so adding size moves our share up and the qualification probability
up while diluting the per-contract reward. That is the competitive effect of
our own quote, and it is why the best candidate is usually not the largest.

Unknowns stay unknown
---------------------
Anything we cannot estimate is named in `unknowns` and charged to the
uncertainty allowance rather than silently assumed to be zero. A candidate
whose net is positive ONLY because an unknown was treated as zero is not a
candidate we should prefer, so the allowance scales with how much we do not
know.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

_log = logging.getLogger(__name__)

CENTS = Decimal("100")
ZERO = Decimal("0")

# Charged per resting quote per horizon: API calls, book maintenance, the
# operator's attention. Small, but a strategy whose edge is smaller than its
# operating cost is not an edge.
DEFAULT_OPERATING_COST_PER_QUOTE_USD = Decimal("0.001")

# Fraction of the gross reward held back per named unknown. Three unknowns
# and the reward is discounted by ~45%: deliberately punitive, because the
# failure mode this guards against is a confident number built on blanks.
UNCERTAINTY_PER_UNKNOWN = Decimal("0.18")
MAX_UNCERTAINTY_FRACTION = Decimal("0.75")


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass(frozen=True)
class QuoteCandidate:
    """One two-sided resting state we could choose."""
    size_contracts: int
    yes_bid_cents: Optional[int]
    no_bid_cents: Optional[int]

    @property
    def is_no_quote(self) -> bool:
        return self.size_contracts <= 0

    def label(self) -> str:
        if self.is_no_quote:
            return "no_quote"
        return f"{self.size_contracts}@{self.yes_bid_cents}/{self.no_bid_cents}"


@dataclass(frozen=True)
class Economics:
    """The decomposed expected net for one candidate. Every term is USD over
    `horizon_sec`, and every term is reported even when zero, so a reader can
    see WHICH term killed or carried the quote."""
    candidate: QuoteCandidate
    horizon_sec: float
    expected_reward_usd: Decimal = ZERO
    expected_trading_pnl_usd: Decimal = ZERO      # negative = adverse selection
    expected_fees_usd: Decimal = ZERO
    expected_exit_cost_usd: Decimal = ZERO
    operating_cost_usd: Decimal = ZERO
    uncertainty_allowance_usd: Decimal = ZERO
    capital_usd: Decimal = ZERO
    unknowns: tuple = ()
    notes: tuple = ()

    @property
    def net_usd(self) -> Decimal:
        return (self.expected_reward_usd
                + self.expected_trading_pnl_usd
                - self.expected_fees_usd
                - self.expected_exit_cost_usd
                - self.operating_cost_usd
                - self.uncertainty_allowance_usd)

    @property
    def net_per_capital(self) -> Decimal:
        """Marginal net per dollar committed. This is the comparison that
        matters under a shared account: two markets compete for the same
        $5,000, so the one that earns more PER DOLLAR wins, not the one that
        earns more in total."""
        if self.capital_usd <= ZERO:
            return ZERO
        return self.net_usd / self.capital_usd

    def explain(self) -> dict:
        return {
            "candidate": self.candidate.label(),
            "horizon_sec": self.horizon_sec,
            "expected_reward_usd": float(self.expected_reward_usd),
            "expected_trading_pnl_usd": float(self.expected_trading_pnl_usd),
            "expected_fees_usd": float(self.expected_fees_usd),
            "expected_exit_cost_usd": float(self.expected_exit_cost_usd),
            "operating_cost_usd": float(self.operating_cost_usd),
            "uncertainty_allowance_usd": float(self.uncertainty_allowance_usd),
            "net_usd": float(self.net_usd),
            "capital_usd": float(self.capital_usd),
            "net_per_capital": float(self.net_per_capital),
            "unknowns": list(self.unknowns),
            "notes": list(self.notes),
        }


@dataclass
class Selection:
    """The chosen candidate and why it beat the others."""
    chosen: Economics
    considered: list = field(default_factory=list)
    reason: str = ""

    @property
    def should_quote(self) -> bool:
        return not self.chosen.candidate.is_no_quote

    def explain(self) -> dict:
        return {"chosen": self.chosen.explain(), "reason": self.reason,
                "considered": [e.explain() for e in self.considered]}


def _market_yield(*, market_id: str, our_size: int, top_book_size: int,
                  target_size: float, discount_factor: float,
                  pool_per_day_usd: float, hours_to_settle: float,
                  midpoint: float, calibration: float,
                  observed_share: Optional[float]):
    from cross_venue.yield_equation import MarketYield
    return MarketYield(
        market_id=market_id,
        pool_per_day=float(pool_per_day_usd),
        our_size=int(our_size),
        top_book_size=int(top_book_size),
        target_size=int(target_size),
        discount_factor=float(discount_factor),
        hours_to_settle=float(hours_to_settle),
        midpoint=float(midpoint),
        calibration=float(calibration),
        observed_share=observed_share,
    )


def evaluate(candidate: QuoteCandidate, *,
             market_id: str,
             horizon_sec: float,
             pool_rate_usd_per_sec: float,
             target_size: float,
             discount_factor: float,
             top_book_size: int,
             midpoint: float,
             hours_to_settle: float,
             calibration: float,
             observed_share: Optional[float] = None,
             expected_fills_per_horizon: Optional[float] = None,
             fee_schedule=None,
             exit_policy=None,
             operating_cost_usd: Decimal = DEFAULT_OPERATING_COST_PER_QUOTE_USD,
             ) -> Economics:
    """Expected net for one candidate over `horizon_sec`.

    `expected_fills_per_horizon` is how many times we expect to be filled.
    It is genuinely unknown until we have our own fill history, so None is
    an accepted input: it is recorded as an unknown and priced through the
    uncertainty allowance rather than assumed to be zero — assuming zero
    fills would make every quote look free of both fees and inventory.
    """
    unknowns: list[str] = []
    notes: list[str] = []

    if candidate.is_no_quote:
        # The baseline every other candidate must beat. Not quoting earns
        # nothing and costs nothing, and it is always available.
        return Economics(candidate=candidate, horizon_sec=horizon_sec,
                         notes=("baseline: no exposure, no reward",))

    if candidate.yes_bid_cents is None or candidate.no_bid_cents is None:
        return Economics(candidate=candidate, horizon_sec=horizon_sec,
                         unknowns=("one-sided quote cannot qualify",),
                         notes=("refused: LIP requires both sides",))

    size = int(candidate.size_contracts)
    days = max(horizon_sec, 1.0) / 86400.0

    # ── reward and adverse selection, from the research model ────────────
    my = _market_yield(
        market_id=market_id, our_size=size, top_book_size=top_book_size,
        target_size=target_size, discount_factor=discount_factor,
        pool_per_day_usd=float(pool_rate_usd_per_sec) * 86400.0,
        hours_to_settle=hours_to_settle, midpoint=midpoint,
        calibration=calibration, observed_share=observed_share,
    )
    # GROSS reward: MarketYield.expected_daily_rebate already nets the
    # adverse cost, so we recompose the gross and subtract adverse ONCE.
    gross_daily = (my.pool_per_day * my.our_share * my.qualify_prob
                   * my.time_factor * my.calibration * my.series_priority)
    expected_reward = _d(gross_daily) * _d(days)

    # Trading P&L. For a passive maker this is the adverse-selection cost:
    # we are filled when the market is moving against the side we showed.
    # This IS the trading-P&L term; nothing else subtracts adverse selection.
    adverse = _d(my.adverse_cost_per_day) * _d(days)
    expected_trading_pnl = -adverse
    notes.append("trading P&L term is the adverse-selection estimate "
                 "(not double-counted elsewhere)")

    if observed_share is None:
        unknowns.append("observed_share (using theoretical depth share)")

    # ── fees ─────────────────────────────────────────────────────────────
    fills = expected_fills_per_horizon
    if fills is None:
        unknowns.append("expected_fills_per_horizon")
        # A bounded placeholder ONLY so fees and exit costs are not silently
        # zero. It is not a forecast; the unknown is declared above and the
        # uncertainty allowance charges for it.
        fills = 1.0
        notes.append("fill rate unknown: fees/exit priced at 1 fill, "
                     "declared as an unknown rather than assumed zero")
    avg_price = int(round((candidate.yes_bid_cents + (100 - candidate.no_bid_cents)) / 2))
    avg_price = min(99, max(1, avg_price))
    fee_per_fill = ZERO
    if fee_schedule is not None:
        try:
            fee_per_fill = _d(fee_schedule.fee_usd(avg_price, size, is_taker=False))
        except Exception as e:
            unknowns.append(f"fee schedule unusable ({e})")
    else:
        unknowns.append("fee schedule unavailable")
    expected_fees = fee_per_fill * _d(fills)

    # ── inventory / exit cost ────────────────────────────────────────────
    # A fill leaves inventory that must be unwound. The cheapest unwind is
    # completing a pair at the opposite bid; its cost is the premium we pay
    # plus the fee on that second fill. Premium is NOT a loss (the pair
    # settles at $1), so only the fee and the spread paid are charged.
    exit_fee = ZERO
    if fee_schedule is not None:
        try:
            exit_fee = _d(fee_schedule.fee_usd(avg_price, size, is_taker=False))
        except Exception:
            exit_fee = ZERO
    # Spread cost of pairing: what we give up crossing from our bid to the
    # opposing bid, per contract, in dollars.
    implied_cost_cents = candidate.yes_bid_cents + candidate.no_bid_cents - 100
    spread_cost = (_d(max(0, implied_cost_cents)) / CENTS) * _d(size)
    expected_exit = (exit_fee + spread_cost) * _d(fills)
    if exit_policy is None:
        notes.append("exit priced as passive pairing (no forced liquidation)")

    # ── operating cost ───────────────────────────────────────────────────
    operating = _d(operating_cost_usd)

    # ── uncertainty allowance ────────────────────────────────────────────
    frac = min(MAX_UNCERTAINTY_FRACTION,
               UNCERTAINTY_PER_UNKNOWN * _d(len(unknowns)))
    allowance = expected_reward * frac

    # ── capital committed ────────────────────────────────────────────────
    # Both legs rest simultaneously; each is fully collateralised.
    capital = ((_d(candidate.yes_bid_cents) + _d(candidate.no_bid_cents))
               / CENTS) * _d(size)

    return Economics(
        candidate=candidate, horizon_sec=horizon_sec,
        expected_reward_usd=expected_reward,
        expected_trading_pnl_usd=expected_trading_pnl,
        expected_fees_usd=expected_fees,
        expected_exit_cost_usd=expected_exit,
        operating_cost_usd=operating,
        uncertainty_allowance_usd=allowance,
        capital_usd=capital,
        unknowns=tuple(unknowns), notes=tuple(notes),
    )


def select(candidates: Sequence[QuoteCandidate], *,
           available_capital_usd: Decimal,
           max_market_capital_usd: Optional[Decimal] = None,
           **kw) -> Selection:
    """Choose the best candidate, including not quoting.

    Ranking is by net per dollar of capital, because markets compete for one
    shared account. Absolute net breaks ties. A candidate is discarded if it
    does not fit the capital that is actually free, so the choice respects
    the account rather than assuming it.
    """
    evaluated: list[Economics] = []
    no_quote: Optional[Economics] = None
    for c in candidates:
        e = evaluate(c, **kw)
        if c.is_no_quote:
            no_quote = e
        evaluated.append(e)
    if no_quote is None:
        no_quote = evaluate(QuoteCandidate(0, None, None), **kw)
        evaluated.append(no_quote)

    feasible: list[Economics] = []
    for e in evaluated:
        if e.candidate.is_no_quote:
            continue
        if e.capital_usd > _d(available_capital_usd):
            continue
        if (max_market_capital_usd is not None
                and e.capital_usd > _d(max_market_capital_usd)):
            continue
        feasible.append(e)

    # Must beat doing nothing on an absolute basis first.
    profitable = [e for e in feasible if e.net_usd > no_quote.net_usd]
    if not profitable:
        reason = "no candidate beats not quoting"
        if feasible:
            best = max(feasible, key=lambda e: e.net_usd)
            reason = (f"best candidate {best.candidate.label()} nets "
                      f"${best.net_usd:.4f} <= no-quote ${no_quote.net_usd:.4f}")
        elif len(evaluated) > 1:
            reason = "no candidate fits available capital"
        return Selection(chosen=no_quote, considered=evaluated, reason=reason)

    best = max(profitable, key=lambda e: (e.net_per_capital, e.net_usd))
    return Selection(
        chosen=best, considered=evaluated,
        reason=(f"{best.candidate.label()} nets ${best.net_usd:.4f} "
                f"(${best.net_per_capital:.4f}/$ of ${best.capital_usd:.2f}) "
                f"vs no-quote ${no_quote.net_usd:.4f}"))
