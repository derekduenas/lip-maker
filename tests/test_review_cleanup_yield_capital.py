"""Review (2026-10-01): MarketYield.capital_at_risk for two resting bids.

A two-sided LIP quote rests a YES bid near p and a NO bid near 1-p. Both
are fully collateralised, so the capital tied up is size*p + size*(1-p),
not size*max(p, 1-p). The old figure understated capital (2x at p=0.5),
overstating yield_pct_daily.
"""
from __future__ import annotations

import pytest

from cross_venue.yield_equation import MarketYield


def _y(mid):
    return MarketYield(market_id="X", pool_per_day=100, our_size=100,
                       top_book_size=100, target_size=200, discount_factor=0.5,
                       hours_to_settle=24, midpoint=mid, calibration=0.25)


@pytest.mark.parametrize("mid", [0.5, 0.3, 0.1, 0.9])
def test_capital_is_both_legs(mid):
    y = _y(mid)
    assert y.capital_at_risk == pytest.approx(100 * (mid + (1 - mid)))
    assert y.yield_pct_daily == pytest.approx(y.expected_daily_rebate / 100.0)
