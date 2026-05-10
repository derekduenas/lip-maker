"""Continuous Kalshi market crawler.

Periodically pulls /markets, filters to status=active, and yields
candidate (market, brain) pairs to the prediction pipeline.

PHASE 1 STUB. Full implementation in Phase 5 (paper-mode scanner).
"""
from __future__ import annotations

from typing import Iterator


def crawl_active_markets() -> Iterator[dict]:
    """Yield active Kalshi market dicts. Phase 5 fills this in."""
    raise NotImplementedError("crawler lands in Phase 5")
