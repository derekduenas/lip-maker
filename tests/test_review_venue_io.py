"""Review fixes (venue area): venue I/O parsing, env loading, paper fills,
read-only allowlists, tick-size exclusion, calibration."""
from decimal import Decimal

import pytest


# ------------------------------------------------------------------ item 10
def test_to_cents_branches_on_type_not_magnitude():
    from mm.venues.kalshi import _to_cents, resting_quote
    assert _to_cents(1) == 1            # legacy int cents: 1c, not $1
    assert _to_cents(45) == 45
    assert _to_cents("45") == 45
    assert _to_cents("0.45") == 45
    assert _to_cents("0.0100") == 1
    assert _to_cents(Decimal("0.45")) == 45
    assert _to_cents(0.45) == 45
    assert _to_cents("1.0000") == 100
    for bad in (True, "45.5", 45.5, "abc"):
        with pytest.raises((ValueError, TypeError)):
            _to_cents(bad)
    assert resting_quote({"book_side": "bid", "yes_price": 1}) == ("yes", 1)
    assert resting_quote({"book_side": "ask", "yes_price": 1}) == ("no", 99)
    assert resting_quote({"book_side": "bid", "yes_price_dollars": "0.0100"}) == ("yes", 1)
