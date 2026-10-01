"""Cross-venue hedge / unwind for one-sided inventory. Paper only.

A Kalshi fill and a PM US fill are not a hedge unless the contracts settle
on the same source, strike and timestamp, and postponement rules match.
Unlisted pairs are denied. The decision is always logged. This module does
not submit an order; ``MAKER_ONLY_ENFORCEMENT_VERIFIED`` stays false and a
taker hedge is the opposite of a maker order.

When a hedge is denied or too expensive, the action is a passive unwind:
quote only the reducing side. A simulated taker hedge records the price and
the fee it would have paid so the daily report can show the counterfactual.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

_log = logging.getLogger("mm.hedge")

ZERO = Decimal("0")


@dataclass(frozen=True)
class Equivalence:
    kalshi_ticker: str
    pmus_slug: str
    same_source: bool
    same_strike: bool
    same_timestamp: bool
    postponement_compatible: bool

    @property
    def allowed(self) -> bool:
        return (self.same_source and self.same_strike and self.same_timestamp
                and self.postponement_compatible)


@dataclass(frozen=True)
class HedgeDecision:
    action: str          # hold | unwind_passive | hedge_simulated
    reason: str
    market: str
    contracts: int
    paper: bool = True
    simulated_price_cents: Optional[int] = None
    simulated_fee_usd: Decimal = ZERO
    log_line: str = ""


@dataclass
class HedgeManager:
    """Paper hedge book. ``equivalences`` is an explicit allow-list."""
    equivalences: dict[tuple[str, str], Equivalence] = field(default_factory=dict)
    decisions: list[HedgeDecision] = field(default_factory=list)
    soft_cap_contracts: float = 50.0

    def allow(self, eq: Equivalence) -> None:
        self.equivalences[(eq.kalshi_ticker, eq.pmus_slug)] = eq

    def lookup(self, kalshi_ticker: str, pmus_slug: str) -> Optional[Equivalence]:
        return self.equivalences.get((kalshi_ticker, pmus_slug))

    def decide(self, *, market: str, net_yes: float, toxic: bool,
               fair_cents: float, hedge_price_cents: Optional[int],
               taker_fee_usd: Decimal, expected_loss_usd: Decimal,
               pmus_slug: str = "") -> HedgeDecision:
        """Choose hold, passive unwind, or a simulated cross-venue hedge.

        Hedge only when |inventory| exceeds the soft cap, the fill is toxic,
        the pair is on the allow-list, and the hedge cost (distance from
        fair plus the taker fee) is strictly less than the expected markout
        loss of holding.
        """
        mag = abs(float(net_yes))
        if mag < self.soft_cap_contracts or not toxic:
            decision = HedgeDecision("hold", "inside_soft_cap_or_not_toxic", market, 0)
            return self._log(decision)
        eq = self.lookup(market, pmus_slug) if pmus_slug else None
        if eq is None or not eq.allowed:
            decision = HedgeDecision(
                "unwind_passive",
                "no_settlement_equivalent_pair" if eq is None else "equivalence_denied",
                market, int(mag),
            )
            return self._log(decision)
        if hedge_price_cents is None:
            decision = HedgeDecision("unwind_passive", "no_hedge_price", market, int(mag))
            return self._log(decision)
        distance = abs(Decimal(hedge_price_cents) - Decimal(str(fair_cents))) / Decimal(100)
        cost = distance * Decimal(int(mag)) + taker_fee_usd
        if cost < expected_loss_usd:
            decision = HedgeDecision(
                "hedge_simulated",
                f"cost {cost:.4f} < expected_loss {expected_loss_usd:.4f}",
                market, int(mag),
                simulated_price_cents=int(hedge_price_cents),
                simulated_fee_usd=taker_fee_usd,
            )
        else:
            decision = HedgeDecision(
                "unwind_passive",
                f"hedge_cost {cost:.4f} >= expected_loss {expected_loss_usd:.4f}",
                market, int(mag),
            )
        return self._log(decision)

    def _log(self, decision: HedgeDecision) -> HedgeDecision:
        line = (f"HEDGE paper action={decision.action} market={decision.market} "
                f"contracts={decision.contracts} reason={decision.reason} "
                f"px={decision.simulated_price_cents} fee={decision.simulated_fee_usd}")
        decision = HedgeDecision(
            decision.action, decision.reason, decision.market, decision.contracts,
            paper=True,
            simulated_price_cents=decision.simulated_price_cents,
            simulated_fee_usd=decision.simulated_fee_usd,
            log_line=line,
        )
        self.decisions.append(decision)
        _log.info(line)
        return decision
