"""Polymarket maker rebate scoring — quadratic spread function.

PER POLYMARKET DOCS:
  S(v, s) = ((v - s) / v)^2 × b

Where:
  v = max_spread for the market (in cents from midpoint)
  s = order's spread distance from adjusted midpoint (in cents)
  b = size weight (proportional to order size)

Two-sided gate (Q_min):
  midpoint in [0.10, 0.90]:
    Q_min = max(min(Q_yes, Q_no), max(Q_yes/c, Q_no/c))  with c=3
    → single-sided allowed at 1/3 efficiency
  midpoint < 0.10 or > 0.90:
    Q_min = min(Q_yes, Q_no)
    → must be two-sided or score = 0

Final per-market epoch payout:
  Q_normal = Q_min / Σ Q_min       (per snapshot share)
  Q_epoch  = Σ Q_normal             (sum over 10,080 weekly samples)
  payout   = Q_epoch / Σ Q_epoch × period_pool

Sampling: per-minute, 10,080 samples per weekly epoch.
"""
from __future__ import annotations

from dataclasses import dataclass


SINGLE_SIDED_DAMPING = 3.0   # 'c' in the docs — single-sided scores at 1/c
EXTREME_MIDPOINT_LO = 0.10
EXTREME_MIDPOINT_HI = 0.90


@dataclass
class SideQuote:
    """One side of our quote (yes or no, bid or ask)."""
    side:           str        # 'yes' | 'no'
    price:          float      # 0.01 - 0.99
    size_contracts: int


def spread_score(v_max_spread: float, s_distance: float, b_size: float) -> float:
    """Polymarket S(v, s) = ((v - s) / v)^2 × b.

    Args:
      v_max_spread: market's max_spread in cents (e.g. 2.0 for ±2¢)
      s_distance:   our order's distance from adjusted midpoint in cents
      b_size:       size weight (proportional to order contracts)

    Returns 0 if order is OUTSIDE max_spread (no rebate eligible).
    """
    if v_max_spread <= 0 or s_distance < 0:
        return 0.0
    if s_distance >= v_max_spread:
        return 0.0
    factor = (v_max_spread - s_distance) / v_max_spread
    return (factor ** 2) * b_size


def compute_q_min(q_yes: float, q_no: float, midpoint: float) -> float:
    """Two-sided gate. Returns the single-snapshot score we earn.

    For midpoint in [0.10, 0.90], single-sided liquidity scores at 1/c
    of the two-sided rate. Outside that band, single-sided = 0.
    """
    if q_yes <= 0 and q_no <= 0:
        return 0.0
    if EXTREME_MIDPOINT_LO <= midpoint <= EXTREME_MIDPOINT_HI:
        # Allow single-sided at damped rate
        return max(min(q_yes, q_no), max(q_yes / SINGLE_SIDED_DAMPING,
                                          q_no / SINGLE_SIDED_DAMPING))
    # Extreme midpoints: must be two-sided
    return min(q_yes, q_no)


def estimate_per_snapshot_share(our_q_min: float, total_q_min: float) -> float:
    """Q_normal = Q_min / Σ Q_min."""
    if total_q_min <= 0:
        return 0.0
    return our_q_min / total_q_min


def estimate_daily_payout(our_q_min_avg: float,
                          total_q_min_avg: float,
                          daily_reward_pool: float) -> float:
    """Estimate daily payout assuming our share is constant over the day.

    daily_reward_pool: $/day for the market (rewards_daily_rate).
    Returns expected $/day for us.
    """
    share = estimate_per_snapshot_share(our_q_min_avg, total_q_min_avg)
    return share * daily_reward_pool


def build_quote_at_best(yes_bid: float, yes_ask: float,
                        max_spread_cents: float,
                        min_size: int,
                        target_size: int | None = None) -> tuple[SideQuote, SideQuote, float]:
    """Build a two-sided quote at-best (price-improving by 0).

    Returns (yes_side, no_side, midpoint_dollars).

    yes_side bids YES at yes_bid (or just inside).
    no_side bids NO at (1 - yes_ask) — i.e., the implied no-bid level.
    """
    midpoint = (yes_bid + yes_ask) / 2.0
    size = target_size if target_size is not None else max(min_size, 50)
    yes_q = SideQuote(side="yes", price=round(yes_bid, 3), size_contracts=size)
    no_q  = SideQuote(side="no",  price=round(1.0 - yes_ask, 3), size_contracts=size)
    return yes_q, no_q, midpoint


def score_our_quote(yes_q: SideQuote, no_q: SideQuote,
                    midpoint: float, max_spread_cents: float) -> dict:
    """Compute our per-snapshot Q_min for one quote pair."""
    # Distance from midpoint in CENTS (not dollars)
    yes_distance = abs((midpoint - yes_q.price) * 100)
    no_distance  = abs(((1.0 - midpoint) - no_q.price) * 100)
    s_yes = spread_score(max_spread_cents, yes_distance, yes_q.size_contracts)
    s_no  = spread_score(max_spread_cents, no_distance,  no_q.size_contracts)
    q_min = compute_q_min(s_yes, s_no, midpoint)
    return {
        "yes_score":   round(s_yes, 4),
        "no_score":    round(s_no, 4),
        "q_min":       round(q_min, 4),
        "yes_distance_cents": round(yes_distance, 3),
        "no_distance_cents":  round(no_distance, 3),
    }
