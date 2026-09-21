"""When to stop holding inventory a maker never wanted (2026-09-21).

The gap
-------
run_paper.py contained no `liquidat*`, `unwind`, `flatten` or `exit_position`
logic of any kind. Inventory acquired by a fill was held until the market
settled. Passive pairing existed only in research/maker_replay.py, which the
runner never imports. So in the operating loop there was neither an active
exit nor a passive one, and no maximum holding time.

That matters more for this strategy than for most. A LIP maker earns a small
per-second rebate and accepts adverse selection in exchange. The rebate is
bounded; the directional loss on inventory is not. Holding to settlement
converts a market-making business into a series of unhedged directional bets
nobody sized.

What "exposure" means on a binary venue
---------------------------------------
You cannot short on Kalshi; you buy YES or you buy NO. Holding equal
quantities of both is RISKLESS at settlement — exactly one pays $1 — so the
position that carries risk is the net:

    net = yes_qty - no_qty       (positive = long YES)

Matched pairs are not risk, they are capital locked up. So the exit policy
targets |net|, and the cheapest way to reduce |net| is often to BUY the
opposite side (completing a pair) rather than to sell, because buying can be
done passively at the bid where we are already quoting, while selling
immediately means crossing.

Rules
-----
1. Age. Inventory older than `max_holding_sec` should go. A maker holding
   overnight is no longer making a market.
2. Size. |net| above `max_net_contracts` should be reduced regardless of age.
3. Cost awareness. Never pay more to exit than the exposure is worth. A
   1-contract net at 50c risks at most $0.50; paying $0.36 of round-trip fee
   to flatten it is worse than holding.
4. Reduce only. An exit may never increase |net| or open a new position.
   This is what separates "unwind" from "double down", and it is checked
   rather than assumed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

_log = logging.getLogger(__name__)

ZERO = Decimal("0")
CENTS = Decimal("100")


@dataclass(frozen=True)
class Position:
    """Net inventory in one market."""
    market_ticker: str
    yes_qty: Decimal
    no_qty: Decimal
    oldest_fill_ts: float          # epoch of the oldest open lot

    @property
    def net_yes(self) -> Decimal:
        """Positive = long YES, negative = long NO, zero = fully paired."""
        return self.yes_qty - self.no_qty

    @property
    def paired(self) -> Decimal:
        """Matched contracts: riskless at settlement, but capital locked."""
        return min(self.yes_qty, self.no_qty)

    @property
    def exposed_side(self) -> Optional[str]:
        n = self.net_yes
        if n > 0:
            return "yes"
        if n < 0:
            return "no"
        return None

    def age_sec(self, now: float) -> float:
        return max(0.0, now - self.oldest_fill_ts)


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    # How to reduce |net|: buy `qty` of `side` to complete pairs.
    side: Optional[str] = None
    qty: Decimal = ZERO
    limit_price_cents: Optional[int] = None
    estimated_cost_usd: Decimal = ZERO
    aggressive: bool = False       # True = cross the spread to get out now

    def __bool__(self) -> bool:
        return self.should_exit


@dataclass(frozen=True)
class ExitPolicy:
    """Thresholds. Deliberately conservative defaults."""
    max_holding_sec: float = 3600.0      # an hour is already a long time for a maker
    max_net_contracts: Decimal = Decimal("25")
    # Past this age, stop waiting for a passive fill and cross.
    aggressive_after_sec: float = 7200.0
    # Never spend more than this fraction of the exposure's notional to exit.
    max_exit_cost_fraction: Decimal = Decimal("0.25")

    def evaluate(self, pos: Position, *, now: float,
                 best_yes_bid_cents: Optional[int],
                 best_no_bid_cents: Optional[int],
                 fee_schedule=None) -> ExitDecision:
        """Decide whether and how to reduce this position's net exposure.

        The returned action always REDUCES |net|: it buys the side we are
        short of. That completes a pair, which is riskless at settlement, and
        can be done passively at the bid.
        """
        net = pos.net_yes
        if net == ZERO:
            return ExitDecision(False, "flat")
        side_held = pos.exposed_side
        # To reduce a long-YES net we buy NO, and vice versa.
        buy_side = "no" if side_held == "yes" else "yes"
        qty = abs(net)
        age = pos.age_sec(now)

        too_old = age >= self.max_holding_sec
        too_big = qty > self.max_net_contracts
        if not (too_old or too_big):
            return ExitDecision(False, f"within limits (age {age:.0f}s, net {qty})")

        price = best_no_bid_cents if buy_side == "no" else best_yes_bid_cents
        if price is None:
            return ExitDecision(False, "no book on the reducing side; cannot price an exit")

        aggressive = age >= self.aggressive_after_sec
        # Passive: join the bid we would be quoting anyway. Aggressive: pay
        # up one cent to actually get done.
        limit = int(price) + (1 if aggressive else 0)
        if not (0 < limit < 100):
            return ExitDecision(False, f"exit price {limit}c off-grid")

        cost = (Decimal(limit) / CENTS) * qty
        fee = ZERO
        if fee_schedule is not None:
            try:
                fee = fee_schedule.fee_usd(limit, qty, is_taker=aggressive)
            except Exception:
                fee = ZERO

        # Cost awareness. The exposure's worst case is qty x $1 if the held
        # side settles worthless, but its MARKET value is what we would pay
        # to close, so compare the fee against the exposure notional.
        notional = (Decimal(limit) / CENTS) * qty
        if notional > ZERO and fee / notional > self.max_exit_cost_fraction:
            return ExitDecision(
                False,
                f"exit fee ${fee:.4f} exceeds {self.max_exit_cost_fraction:%} of "
                f"${notional:.4f} exposure — holding is cheaper")

        reason = "max_holding_age" if too_old else "max_net_exposure"
        if aggressive:
            reason += "_aggressive"
        return ExitDecision(True, reason, side=buy_side, qty=qty,
                            limit_price_cents=limit,
                            estimated_cost_usd=cost + fee,
                            aggressive=aggressive)


def reduces_exposure(pos: Position, side: str, qty) -> bool:
    """Guard: would buying `qty` of `side` reduce |net|?

    Called before acting on any exit so that an "unwind" cannot quietly
    become a larger bet — the distinction the handoff asked for between
    reducing exposure and opening more risk.
    """
    q = Decimal(str(qty))
    if q <= 0:
        return False
    before = abs(pos.net_yes)
    if side == "yes":
        after = abs(pos.net_yes + q)
    elif side == "no":
        after = abs(pos.net_yes - q)
    else:
        return False
    return after < before
