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


DECISION_SIGMA = 0.0007  # 7bp — empirically calibrated to match CME FedWatch
                          # (Jun 17 2026 next-meeting parity: 5.6%/94.4% vs CME 6.4%/93.6%
                          #  within 0.8pp; spec said "try 12.5bp" but 12.5bp overshot to 23.7%)


def decision_probs(zq_price: float, ctx: FOMCContext,
                   sigma: float = DECISION_SIGMA) -> dict[float, float]:
    """Return {bucket_lower_bound: probability} for ctx.decision_buckets.

    Gaussian-smoothed FedWatch decomposition:
      Each bucket [b, b+25bp] gets the integrated mass of N(implied, sigma²)
      over that interval. sigma=12.5bp by default (half a bucket).

    Why Gaussian smoothing instead of pure linear interp:
      - Pure linear interp + boundary snap puts 100% on the modal bucket
        when implied_rate falls on a midpoint, but CME's published probs
        always leak some mass to adjacent buckets (their tree-based model
        carries variance). Without smoothing, we get systematic ~6pp parity
        errors at exact bucket-center implied rates.
      - sigma=12.5bp reproduces CME's empirical spread reasonably for
        next-meeting comparisons. Tune if forward parity samples drift.

    Tail mass (below lowest bucket or above highest) gets redirected to
    the extreme bucket so probabilities sum to 1 across the decision set.
    """
    from math import erf, sqrt

    implied_post = post_meeting_rate(zq_price, ctx)
    buckets = sorted(ctx.decision_buckets)
    sqrt2 = sqrt(2.0)

    out: dict[float, float] = {}
    for b in buckets:
        lo, hi = b, b + 2 * BUCKET_HALF_WIDTH
        z_lo = (lo - implied_post) / (sigma * sqrt2)
        z_hi = (hi - implied_post) / (sigma * sqrt2)
        out[b] = max(0.0, 0.5 * (erf(z_hi) - erf(z_lo)))

    total = sum(out.values())
    tail = 1.0 - total
    if tail > 1e-6:
        # Mass outside the bucket grid — push to the extreme nearest implied.
        if implied_post < buckets[0] + BUCKET_HALF_WIDTH:
            out[buckets[0]] += tail
        elif implied_post > buckets[-1] + BUCKET_HALF_WIDTH:
            out[buckets[-1]] += tail
        else:
            # interior tail (rare with sigma <= bucket width) — split symmetrically
            for b in out:
                out[b] += tail / len(out)
    elif total > 0 and abs(total - 1.0) > 1e-9:
        for b in out:
            out[b] /= total
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
