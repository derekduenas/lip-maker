"""Market -> brain routing.

For each candidate market, query each registered brain's can_evaluate()
and route to the single claimant. Multi-claim is an error: a market
should belong to exactly one brain.
"""
from __future__ import annotations

from typing import Optional

from argus.brains.base import DomainBrain


def route(
    market_ticker: str,
    registry:      dict[str, DomainBrain],
) -> Optional[DomainBrain]:
    """Return the single brain claiming this ticker, or None."""
    claimants = [b for b in registry.values() if b.can_evaluate(market_ticker)]
    if len(claimants) == 0:
        return None
    if len(claimants) > 1:
        ids = [b.brain_id for b in claimants]
        raise RuntimeError(f"multi-claim on {market_ticker}: {ids}")
    return claimants[0]
