"""Gemini Predictions maker rebate scorer.

OFFICIAL FORMULA (from docs.gemini.com/prediction-markets/maker-rebate-program):

    Rebate = RebateRate × TakerRate × C × P × (1 − P)

Where:
    C           = number of contracts
    P           = execution price in dollars (e.g. 0.50 for 50¢)
    TakerRate   = current taker fee rate (0.07 = 7% per docs example; may vary)
    RebateRate  = program-set multiplier, promo-dependent

Eligibility band: P must be in [$0.20, $0.80]. Outside = zero rebate.
Notional cap:    Rebate ≤ 5% × (C × P), per fill.
Payout:          Daily 5 PM ET in USD.
Applicable to:   Contracts created March 18, 2026 or later.

PROMO RATES (Apr 9 – May 9, 2026):
    Crypto, Commodities markets: 0.70
    Sports markets:              0.50
STANDARD RATES (May 9+):
    All markets:                 0.30

Key insight vs Kalshi: QUADRATIC in P × (1−P), maximized at P = 0.50. This
structurally favors tight-spread quoting near midpoint, not big-size deep
quoting. Competition for rebate is ZERO-SUM across all makers on a market.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# ── Tunables (source: official Gemini docs 2026-04-22) ────────────────

PROMO_END_UTC = datetime(2026, 5, 9, 21, 0, tzinfo=timezone.utc)  # 5PM ET May 9
STANDARD_RATE = 0.30
PROMO_RATES = {
    "crypto":    0.70,
    "commodity": 0.70,
    "sports":    0.50,
}
DEFAULT_TAKER_RATE = 0.07  # 7% per docs example; override from fee schedule
PRICE_MIN = 0.20
PRICE_MAX = 0.80
NOTIONAL_CAP_PCT = 0.05    # 5% cap per fill


@dataclass
class RebateResult:
    rebate_usd:   float
    notional_usd: float
    rate_applied: float
    eligibility:  str       # "eligible" | "out_of_band" | "below_min_price" | "above_max_price"
    cap_hit:      bool      # True if notional cap binding


def rebate_for_fill(price_dollars: float, contracts: int,
                    market_category: str = "commodity",
                    taker_rate: float = DEFAULT_TAKER_RATE,
                    now: Optional[datetime] = None) -> RebateResult:
    """Compute rebate for one maker fill.

    Args:
        price_dollars:   execution price, $0.01-$0.99
        contracts:       number of contracts filled
        market_category: "crypto" | "commodity" | "sports" | other
        taker_rate:      override for the 0.07 default
        now:             override current time for testing
    """
    notional = contracts * price_dollars
    now = now or datetime.now(timezone.utc)

    # Eligibility check
    if price_dollars < PRICE_MIN:
        return RebateResult(0.0, notional, 0.0, "below_min_price", False)
    if price_dollars > PRICE_MAX:
        return RebateResult(0.0, notional, 0.0, "above_max_price", False)

    # Current rate
    in_promo = now < PROMO_END_UTC
    if in_promo:
        rate = PROMO_RATES.get(market_category.lower(), STANDARD_RATE)
    else:
        rate = STANDARD_RATE

    raw = rate * taker_rate * contracts * price_dollars * (1 - price_dollars)
    cap = notional * NOTIONAL_CAP_PCT
    final = min(raw, cap)
    cap_hit = raw > cap

    # Round down to next cent per docs ("Rebates rounded down to the next cent")
    final_cents = int(final * 100)   # floor via int cast on positive
    return RebateResult(final_cents / 100.0, notional, rate, "eligible", cap_hit)


def projected_rebate_per_market(target_share_pct: float,
                                daily_fill_volume_contracts: int,
                                avg_fill_price_dollars: float,
                                market_category: str,
                                taker_rate: float = DEFAULT_TAKER_RATE,
                                now: Optional[datetime] = None) -> float:
    """Estimate daily rebate for one market given expected share + volume.

    Useful for pre-launch ROI modeling.
    """
    our_contracts = int(daily_fill_volume_contracts * target_share_pct)
    r = rebate_for_fill(avg_fill_price_dollars, our_contracts,
                        market_category, taker_rate, now)
    return r.rebate_usd


# ── Self-test with docs example ───────────────────────────────────────

def _self_test():
    """Validate against Gemini's own published example.

    Per docs: 100 contracts at $0.50, commodity, promo → rate 0.70.
    Expected: 0.70 × 0.07 × 100 × 0.50 × 0.50 = $1.225 rebate
    """
    r = rebate_for_fill(
        price_dollars=0.50,
        contracts=100,
        market_category="commodity",
        taker_rate=0.07,
        now=datetime(2026, 4, 22, tzinfo=timezone.utc),  # during promo
    )
    expected_raw = 0.70 * 0.07 * 100 * 0.50 * 0.50   # = 1.225
    print(f"Test: 100@$0.50 commodity, promo")
    print(f"  computed rebate:  ${r.rebate_usd}")
    print(f"  expected (pre-round): ${expected_raw}")
    print(f"  notional: ${r.notional_usd}, rate: {r.rate_applied}, cap_hit: {r.cap_hit}")

    # Out-of-band test
    r2 = rebate_for_fill(price_dollars=0.15, contracts=100,
                          market_category="commodity")
    assert r2.eligibility == "below_min_price"
    assert r2.rebate_usd == 0
    print(f"\nOut-of-band test ($0.15): eligibility={r2.eligibility}, rebate=${r2.rebate_usd} ✓")

    # Cap test — extreme numbers trigger 5% cap
    r3 = rebate_for_fill(price_dollars=0.50, contracts=1_000_000,
                          market_category="commodity",
                          taker_rate=10.0)  # absurd taker rate → cap should bind
    notional = 1_000_000 * 0.50
    cap = notional * 0.05  # $25,000
    print(f"\nCap test: rebate=${r3.rebate_usd}, cap_hit={r3.cap_hit}")


if __name__ == "__main__":
    _self_test()
