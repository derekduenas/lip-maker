"""Inventory-aware reservation price.

Avellaneda and Stoikov (2008), Quantitative Finance 8(3), 217–224, equation
for the indifference price around a reference s:

    r(s, q, t) = s − q γ σ² (T − t)

and a half-spread

    δ/2 = (γ σ² (T − t))/2 + (1/γ) ln(1 + γ/k)

Guéant, Lehalle and Fernandez-Tapia (2013), arXiv:1105.3115, add a hard
inventory bound: at ±q_max the increasing side is not quoted.

Units here are binary-contract units, stated so the formula is not silently
rescaled into equity-years (that rescaling makes the skew a fraction of a
cent on these markets and the price never moves):

    s, r        YES probability in cents
    q           inventory as a fraction of the per-market net cap, in [-1, 1]
                +1 means long YES up to the cap
    σ           recent stdev of the YES fair value, in cents, over one hour
    τ           hours to the quoting horizon, floored at one minute
    γ           risk aversion per cent. Default 0.04, so a full long, σ = 5¢
                per √hour, and τ = 1 hour moves the reservation by
                q γ σ² τ = 1 · 0.04 · 25 · 1 = 1 cent.

The half-spread's (1/γ) ln(1+γ/k) term is computed in cents with k = 1.5
per cent (arrival intensity falls by e^1.5 when we demand one more cent).
With γ = 0.04 that logarithmic term is large, so the quoter treats it as a
*ceiling* on how far inside the touch we may step, and the number that
actually moves a LIP quote is the reservation shift applied to the heavy
side's price. Reward scoring punishes distance from the reference price;
widening both sides by the textbook dollar-spread is the wrong objective
on a liquidity-incentive book. The heavy-side price move is the part that
cuts adverse selection without abandoning the reducing side's reward.

At |q| >= 1 the increasing side is suppressed (Guéant bound).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


DEFAULT_GAMMA = 0.04
DEFAULT_K = 1.5


@dataclass(frozen=True)
class Reservation:
    fair_cents: float
    reservation_cents: float
    skew_cents: int             # signed: negative => lower YES bid
    half_spread_cents: int
    suppress_side: Optional[str]  # "yes" | "no" | None
    reason: str


def reservation_cents(fair_cents: float, inventory_fraction: float, *,
                      sigma_cents: float, tau_hours: float,
                      gamma: float = DEFAULT_GAMMA) -> float:
    tau = max(float(tau_hours), 1.0 / 60.0)
    q = max(-1.5, min(1.5, float(inventory_fraction)))
    r = float(fair_cents) - q * float(gamma) * (float(sigma_cents) ** 2) * tau
    return max(1.0, min(99.0, r))


def half_spread_cents(sigma_cents: float, tau_hours: float, *,
                      gamma: float = DEFAULT_GAMMA, k: float = DEFAULT_K) -> int:
    """Textbook AS half-spread, in whole cents, at least 1 when sigma > 0."""
    if sigma_cents <= 0 or k <= 0 or gamma <= 0:
        return 0
    tau = max(float(tau_hours), 1.0 / 60.0)
    raw = 0.5 * gamma * (sigma_cents ** 2) * tau + (1.0 / gamma) * math.log(1.0 + gamma / k)
    return max(1, int(math.floor(raw)))


def quote_reservation(fair_cents: float, net_yes: float, cap_contracts: float, *,
                      sigma_cents: float, tau_hours: float,
                      gamma: float = DEFAULT_GAMMA,
                      max_skew_cents: int = 3) -> Reservation:
    """Price skew for the heavy side, and a suppress flag at the cap.

    ``skew_cents`` is added to the YES bid (and subtracted from the NO bid,
    which is the YES offer). Long YES (positive net) produces a negative
    skew: we bid less for more YES. The magnitude is the whole-cent gap
    between fair and the reservation, capped so a single quiet hour cannot
    walk the quote out of the reward band entirely.
    """
    cap = abs(float(cap_contracts)) or 1.0
    frac = float(net_yes) / cap
    r = reservation_cents(fair_cents, frac, sigma_cents=sigma_cents,
                          tau_hours=tau_hours, gamma=gamma)
    diff = r - float(fair_cents)
    skew = int(math.trunc(diff))  # toward zero; sub-cent does not move a 1c book
    if skew > max_skew_cents:
        skew = max_skew_cents
    if skew < -max_skew_cents:
        skew = -max_skew_cents
    suppress = None
    if frac >= 1.0:
        suppress = "yes"
    elif frac <= -1.0:
        suppress = "no"
    half = half_spread_cents(sigma_cents, tau_hours, gamma=gamma)
    reason = f"q={frac:+.2f} r={r:.2f} fair={fair_cents:.2f} skew={skew:+d}c"
    return Reservation(fair_cents=float(fair_cents), reservation_cents=r,
                       skew_cents=skew, half_spread_cents=half,
                       suppress_side=suppress, reason=reason)


def apply_skew(yes_bid: Optional[int], no_bid: Optional[int],
               skew_cents: int) -> tuple[Optional[int], Optional[int]]:
    """Move both prices so the reservation, not only the size, changes.

    YES bid shifts by ``skew_cents``. NO bid shifts by the opposite amount
    because a higher NO bid is a lower YES offer. Prices stay inside 1..99.
    A missing side stays missing (the caller already suppressed it).
    """
    def _clip(p: Optional[int], delta: int) -> Optional[int]:
        if p is None:
            return None
        return max(1, min(99, int(p) + delta))
    return _clip(yes_bid, skew_cents), _clip(no_bid, -skew_cents)
