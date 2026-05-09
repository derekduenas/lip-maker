"""CME Fed Funds futures → implied P(rate cut) at FOMC.

CME FedWatch methodology, simplified:

  ZQ contracts settle on the average effective fed funds rate (EFFR) for
  the contract month. Contract price = 100 - implied_avg_rate.

  For an FOMC meeting in month M:
    - Pre-FOMC days: rate stays at current target.
    - Post-FOMC days: rate = post-decision target.
    - Implied avg = (pre_days × current_rate + post_days × post_rate) / total_days.

  Solving for post_rate:
    post_rate = (implied_avg × total - pre_days × current) / post_days

  P(cut 25bp) is then derived from the distance from current target:
    P(cut 25bp) = clip( (current - post_rate) / 0.25 , 0, 1 )
    P(no change) = 1 - P(cut 25bp) − P(cut 50bp) − ...

  This is the standard FedWatch decomposition. Two-step assumption:
    only "no change" and "cut 25bp" are possible. Adjust for >25bp moves
    using the next-meeting contract's relative pricing.

REFERENCE: CME Group "FedWatch Tool — Methodology" whitepaper.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class FOMCContext:
    """Inputs needed to convert ZQ price → P(cut)."""
    current_target_lower: float    # e.g. 0.0525 for 5.25%
    current_target_upper: float    # e.g. 0.0550
    fomc_date:            dt.date
    contract_month_start: dt.date
    contract_month_end:   dt.date
    # Possible decisions (target rate AFTER FOMC, in decimal). Order matters
    # only for clarity; probabilities sum to 1 across the listed buckets.
    decision_buckets:     list[float]

    @property
    def current_mid(self) -> float:
        return (self.current_target_lower + self.current_target_upper) / 2.0


def implied_avg_rate(zq_price: float) -> float:
    """ZQ price → implied avg fed funds rate (decimal). 94.75 → 5.25%."""
    return (100.0 - zq_price) / 100.0


def post_meeting_rate(zq_price: float, ctx: FOMCContext) -> float:
    """Solve for the implied post-meeting target rate from ZQ price.

    Math:
        avg = (pre_days × current + post_days × post) / total
    """
    implied_avg = implied_avg_rate(zq_price)
    total_days  = (ctx.contract_month_end - ctx.contract_month_start).days + 1
    pre_days    = max(0, (ctx.fomc_date - ctx.contract_month_start).days)
    post_days   = max(1, total_days - pre_days)  # avoid div by zero
    current     = ctx.current_mid
    post = (implied_avg * total_days - pre_days * current) / post_days
    return post


# Each bucket is a 25bp target RANGE; we interpolate against the bucket
# MIDPOINT (= lower_bound + 12.5bp). This matches CME FedWatch convention.
BUCKET_HALF_WIDTH = 0.00125  # 12.5bp


def decision_probs(zq_price: float, ctx: FOMCContext) -> dict[float, float]:
    """Return {bucket_lower_bound: probability} for ctx.decision_buckets.

    FedWatch decomposition:
      Each bucket represents a 25bp target range. The implied post-meeting
      rate (continuous) is decomposed onto bucket MIDPOINTS via linear
      interpolation between adjacent bucket centers.
    """
    implied_post = post_meeting_rate(zq_price, ctx)
    buckets = sorted(ctx.decision_buckets)
    # Map lower-bound → midpoint for interp; remember reverse mapping.
    midpoints = [b + BUCKET_HALF_WIDTH for b in buckets]

    # Edge: implied at/below lowest midpoint → all weight on lowest bucket.
    if implied_post <= midpoints[0]:
        return {b: (1.0 if b == buckets[0] else 0.0) for b in buckets}
    # Edge: implied at/above highest midpoint → all weight on highest bucket.
    if implied_post >= midpoints[-1]:
        return {b: (1.0 if b == buckets[-1] else 0.0) for b in buckets}

    # Find adjacent bucket midpoints bracketing the implied rate.
    out = {b: 0.0 for b in buckets}
    for i in range(len(midpoints) - 1):
        lo_mid, hi_mid = midpoints[i], midpoints[i + 1]
        if lo_mid <= implied_post <= hi_mid:
            span = hi_mid - lo_mid
            w_hi = (implied_post - lo_mid) / span if span > 0 else 0.5
            w_lo = 1.0 - w_hi
            out[buckets[i]]     = w_lo
            out[buckets[i + 1]] = w_hi
            break
    return out


def implied_prob_kalshi_question(
    zq_price: float,
    ctx: FOMCContext,
    kalshi_target_rate: float,
) -> float:
    """P(Kalshi market resolves YES) implied by ZQ futures.

    Kalshi rate-decision markets are typically of the form:
      "Will Fed set target rate to X.XX-X.XX% at the M FOMC?"
    where X.XX-X.XX is one of the buckets. We map kalshi_target_rate to
    the closest bucket and read its probability.
    """
    probs = decision_probs(zq_price, ctx)
    # Snap to nearest bucket (Kalshi uses lower bound of range typically).
    nearest = min(probs.keys(), key=lambda b: abs(b - kalshi_target_rate))
    return probs[nearest]


# ── Self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Hypothetical: current target 5.25-5.50%, FOMC June 18 2026, ZQ June at 94.75.
    ctx = FOMCContext(
        current_target_lower=0.0525,
        current_target_upper=0.0550,
        fomc_date=dt.date(2026, 6, 18),
        contract_month_start=dt.date(2026, 6, 1),
        contract_month_end=dt.date(2026, 6, 30),
        decision_buckets=[0.0500, 0.0525, 0.0550],  # cut 25, hold, hike 25
    )

    # ZQ at 94.75 → implied avg 5.25% → suggests a cut already priced in.
    zq = 94.75
    probs = decision_probs(zq, ctx)
    print(f"ZQ={zq}: {probs}")
    # Implied avg 5.25%, current 5.375%, fomc June 18:
    #   pre_days=17, post_days=13, total=30.
    #   post = (0.0525×30 - 17×0.0537) / 13 ≈ 0.0510
    # That snaps between 5.00 (cut 25) and 5.25 (hold) → expect mass on both.
    post = post_meeting_rate(zq, ctx)
    print(f"implied post-meeting target: {post*100:.3f}%")

    # YES question: "Fed cuts to 5.00-5.25%?"
    p_yes = implied_prob_kalshi_question(zq, ctx, kalshi_target_rate=0.0500)
    print(f"P(cut to 5.00%) implied by ZQ: {p_yes:.3f}")
    assert 0.0 <= p_yes <= 1.0
    assert sum(probs.values()) == 1.0 or abs(sum(probs.values()) - 1.0) < 1e-9

    # Edge case: ZQ at 94.625 → implied avg 5.375% (current rate) → expect hold.
    probs_hold = decision_probs(94.625, ctx)
    print(f"ZQ=94.625 (no move priced): {probs_hold}")
    assert probs_hold[0.0525] > 0.5

    print("fed_funds self-test OK")
