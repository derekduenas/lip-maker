"""Avellaneda-Stoikov reservation price (Track A.2).

The classic A-S formulation (Avellaneda & Stoikov 2008,
https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf):

    r(s, q, t) = s − q·γ·σ²·(T−t)

where:
    s    fair-value reference  (we use the microprice from A.1)
    q    signed inventory (positive = long the asset)
    γ    risk-aversion knob
    σ²   variance of fair value
    T-t  time remaining to settle

The reservation price is the maker's *indifference midpoint* — quoting
both sides symmetric around `r` rather than around `s` biases fills
toward the inventory-balancing side.

KALSHI-SPECIFIC TWIST
---------------------
The full A-S half-spread formula widens the spread to compensate for
risk-aversion. We do NOT widen the spread for Kalshi LIP markets,
because Kalshi's LIP scoring formula is:

    Score = Size × DiscountFactor^(ReferencePrice − Price)

with ReferencePrice = best bid. Moving our quote price below best-bid
multiplies our score by `DiscountFactor^(distance)` — a real cost on
markets with low DiscountFactor (e.g. 0.5, top-of-book only). So:

  - When DiscountFactor ≥ 0.7 (depth-friendly):
    we can absorb a 1-tick price skew on the inventory-heavy side
    without devastating share. The skew reduces the chance the next
    fill adds to a heavy position.
  - When DiscountFactor < 0.7 (top-of-book only):
    we must quote at best-bid; A.2 inverts the existing size-skew
    (already present) but does not move price.

This module exposes `reservation_price()` for r, plus
`suggest_quote_skew()` which returns recommended yes/no tick offsets
respecting DiscountFactor.

USAGE
-----
    from engine.reservation_price import reservation_price, suggest_quote_skew
    r = reservation_price(mp_cents=mp, net_inventory=net,
                          gamma=settings.AS_GAMMA, sigma_cents=sigma,
                          hours_to_settle=hrs)
    skew = suggest_quote_skew(mp_cents=mp, r_cents=r,
                              discount_factor=df, max_tick_offset=1)
    # skew = (yes_offset_c, no_offset_c)  e.g. (-1, 0) → back YES 1c
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

# Threshold: only apply price-skew when DiscountFactor at least this high.
# Below this, the per-tick share loss exceeds the adverse-selection saving.
DF_PRICE_SKEW_THRESHOLD = 0.70


def reservation_price(
    mp_cents: float,
    net_inventory: int,
    gamma: float,
    sigma_cents: float,
    hours_to_settle: float,
) -> float:
    """A-S reservation price in cents.

    Inputs use the natural Kalshi units:
      - mp_cents in [1, 99]
      - net_inventory signed (+ = long YES)
      - sigma_cents = stdev of YES price in cents over a recent window
      - hours_to_settle in hours

    Internally converts (T-t) to years so γ, σ have the standard A-S
    dimensions. With γ ≈ 0.1, σ ≈ 5c, net ≈ 100, T-t = 24h:
        skew = 100 × 0.1 × 25 × (24/8760) ≈ 0.68c
    """
    if mp_cents is None:
        return float("nan")
    years = max(1.0 / 8760.0, float(hours_to_settle) / 8760.0)  # ≥ 1h floor
    sigma_sq = float(sigma_cents) ** 2
    r = float(mp_cents) - float(net_inventory) * float(gamma) * sigma_sq * years
    return max(1.0, min(99.0, r))


def realized_sigma_cents(samples: Sequence[float]) -> float:
    """Sample stdev of a sequence of YES-side cents observations.

    Returns 0.0 if fewer than 2 samples — caller can treat as "no signal".
    """
    if samples is None:
        return 0.0
    xs = [float(x) for x in samples if x is not None]
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


@dataclass
class QuoteSkew:
    yes_tick_offset: int   # 0 or negative (we never join further out than best)
    no_tick_offset:  int
    reason:          str   # human-readable explanation
    r_minus_mp_c:    float


def suggest_quote_skew(
    mp_cents: float,
    r_cents: float,
    discount_factor: float,
    max_tick_offset: int = 1,
    apply_threshold_c: float = 0.5,
) -> QuoteSkew:
    """Translate (mp, r) into per-side tick offsets respecting DF gate.

    Sign convention: yes_tick_offset is the cents to SUBTRACT from
    best_yes_bid before placing (i.e. -1 = quote 1c WORSE than best
    on YES). Similarly for no_tick_offset.

    Logic:
      - If r > mp + threshold: market wants us to be more aggressive on
        YES (long-NO inventory). Stay at best on YES, back NO 1 tick.
      - If r < mp - threshold: opposite. Stay at best on NO, back YES.
      - |r - mp| ≤ threshold: no skew.
      - If discount_factor < DF_PRICE_SKEW_THRESHOLD: zero out skews
        (back-off costs too much share at this DF). Reason will note it.
    """
    if mp_cents is None or r_cents is None or math.isnan(r_cents):
        return QuoteSkew(0, 0, "no_microprice", 0.0)

    diff = float(r_cents) - float(mp_cents)
    abs_off = min(max_tick_offset, int(abs(diff) // 1.0))

    if abs(diff) < apply_threshold_c:
        return QuoteSkew(0, 0, "below_threshold", diff)

    if discount_factor < DF_PRICE_SKEW_THRESHOLD:
        return QuoteSkew(0, 0,
                         f"df={discount_factor:.2f}<{DF_PRICE_SKEW_THRESHOLD} (size-skew only)",
                         diff)

    if diff > 0:
        # r above mp → we'd rather BUY at lower price → quote NO worse, keep YES tight
        return QuoteSkew(0, -abs_off, f"r-mp={diff:+.2f}c long_NO_back", diff)
    else:
        return QuoteSkew(-abs_off, 0, f"r-mp={diff:+.2f}c long_YES_back", diff)


def _self_test() -> None:
    # Symmetric inventory → no skew
    r = reservation_price(mp_cents=50.0, net_inventory=0,
                          gamma=0.1, sigma_cents=5.0, hours_to_settle=24)
    assert abs(r - 50.0) < 0.01, r

    # Long YES at 100 contracts, 24h, 5c stdev, γ=0.1
    #   skew = 100 × 0.1 × 25 × 24/8760 = 0.685c (below threshold)
    r2 = reservation_price(mp_cents=50.0, net_inventory=100,
                           gamma=0.1, sigma_cents=5.0, hours_to_settle=24)
    assert 49.0 < r2 < 50.0, r2

    # Very long YES 5000 contracts, same params → bigger skew
    r3 = reservation_price(mp_cents=50.0, net_inventory=5000,
                           gamma=0.1, sigma_cents=5.0, hours_to_settle=24)
    assert r3 < 49.0, r3

    # Suggest skew
    s = suggest_quote_skew(mp_cents=50.0, r_cents=48.0,
                           discount_factor=0.9, max_tick_offset=1)
    # r < mp → long YES → back YES, keep NO tight
    assert s.yes_tick_offset == -1 and s.no_tick_offset == 0, s

    # Low DF → no price skew, just reason annotation
    s2 = suggest_quote_skew(mp_cents=50.0, r_cents=48.0,
                            discount_factor=0.5, max_tick_offset=1)
    assert s2.yes_tick_offset == 0 and s2.no_tick_offset == 0, s2
    assert "df=" in s2.reason, s2

    # Below threshold → no skew
    s3 = suggest_quote_skew(mp_cents=50.0, r_cents=50.3,
                            discount_factor=0.9, max_tick_offset=1)
    assert s3.yes_tick_offset == 0 and s3.no_tick_offset == 0, s3

    # Realized sigma
    sig = realized_sigma_cents([48, 50, 51, 49, 52])
    assert 1.0 < sig < 2.5, sig

    print(f"reservation_price self-test: PASS (mp=50, q=100, σ=5, T=24h → "
          f"r={r2:.3f}; q=5000 → r={r3:.3f}; "
          f"σ([48,50,51,49,52])={sig:.3f}c)")


if __name__ == "__main__":
    _self_test()
