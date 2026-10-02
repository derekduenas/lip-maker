"""Patch 18: inventory skew, paper only.

Replaces "pause the filled side for 30 min" (LIP_FILL_COOLDOWN_S) when
LIP_SKEW_ENABLE=1. Given the net unpaired inventory of a market (and its
event), both bids are shifted:

* the side that REDUCES inventory (the opposite side; buying it pairs the
  holding into a $1 settlement) is raised by ``a`` ticks, toward the touch
  but never at/through the opposite implied ask (no crossing);
* the side that ADDS inventory is lowered by ``b`` ticks.

This is a LINEAR-IN-INVENTORY TICK HEURISTIC, not Avellaneda-Stoikov: there
is no risk aversion (gamma), volatility (sigma) or horizon (T - t) term.
q is the fraction of the unpaired-$ cap used (max of market $ /
LIP_MARKET_INV_CAP_USD and event $ / LIP_EVENT_INV_CAP_USD), and
a = ceil(q x LIP_SKEW_MAX_TICKS), b = ceil(q x LIP_SKEW_MAX_BACKOFF), both
rounded UP so any inventory above LIP_SKEW_MIN_FRAC moves both sides by at
least one tick, reaching the maxima at the cap. The caps themselves still
block the adding side outright (unchanged).

Reward cost (Kalshi LIP): an order at/above the reference price (level holding
target/5) earns full credit; each tick below earns DF^ticks. So
* raising the reducing side above the reference costs 0 reward (only capital
  and adverse selection);
* backing off the adding side by b ticks below the reference costs
  (1 - DF^b) of that side's credit. ``b`` is reduced until this loss stays
  <= LIP_SKEW_MAX_REWARD_LOSS (default 0.5 => 1 tick at DF 0.5, none at
  DF 0.3). The side stays in the book (in the reward zone at partial
  credit) instead of being pulled, which kept 0% of that side's credit
  under the old cooldown.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    return _num("LIP_SKEW_ENABLE", 0.0) > 0


@dataclass
class SkewParams:
    max_ticks: int = 2          # aggressive shift of the reducing side at full cap
    max_backoff: int = 1        # back-off of the adding side at full cap
    max_reward_loss: float = 0.5  # max fraction of the adding side's credit given up
    min_frac: float = 0.0       # inventory fraction below which nothing shifts

    @classmethod
    def from_env(cls) -> "SkewParams":
        return cls(
            max_ticks=max(0, int(_num("LIP_SKEW_MAX_TICKS", 2))),
            max_backoff=max(0, int(_num("LIP_SKEW_MAX_BACKOFF", 1))),
            max_reward_loss=min(1.0, max(0.0, _num("LIP_SKEW_MAX_REWARD_LOSS", 0.5))),
            min_frac=max(0.0, _num("LIP_SKEW_MIN_FRAC", 0.0)),
        )


def reward_cost_per_tick(df: float, ticks_below_ref: int) -> list:
    """Fraction of one side's credit lost at 1..n ticks below the reference."""
    return [round(1.0 - float(df) ** t, 6) for t in range(1, int(ticks_below_ref) + 1)]


def skew_prices(yes_c: int, no_c: int, *, net_yes: float, frac: float,
                best_yes: int | None, best_no: int | None, df: float,
                yes_ref: int | None = None, no_ref: int | None = None,
                params: SkewParams | None = None) -> dict:
    """Return {"yes_cents", "no_cents", "agg", "back", "reduce", "add",
    "reward_loss", "frac"}. ``yes_c``/``no_c`` are the unskewed bids (normally
    the reward references). ``net_yes`` = yes contracts - no contracts held.
    ``frac`` = inventory fraction of cap in [0, 1+]."""
    p = params or SkewParams.from_env()
    out = {"yes_cents": int(yes_c), "no_cents": int(no_c), "agg": 0, "back": 0,
           "reduce": None, "add": None, "reward_loss": 0.0, "frac": round(float(frac), 4)}
    if not net_yes or frac <= p.min_frac or frac <= 0:
        return out
    f = min(1.0, float(frac))
    add = "yes" if net_yes > 0 else "no"
    red = "no" if add == "yes" else "yes"
    out["reduce"], out["add"] = red, add
    px = {"yes": int(yes_c), "no": int(no_c)}
    ref = {"yes": yes_ref if yes_ref is not None else int(yes_c),
           "no": no_ref if no_ref is not None else int(no_c)}
    best_opp = {"yes": best_no, "no": best_yes}
    # Aggressive side: ceil so any inventory gets at least one tick (0 reward cost).
    agg = int(math.ceil(f * p.max_ticks - 1e-9)) if p.max_ticks > 0 else 0
    if agg > 0:
        cap = 99 if best_opp[red] is None else 99 - int(best_opp[red])  # never cross
        new = min(px[red] + agg, cap, 99)
        new = max(new, px[red])  # never move the reducing side away because of the cap
        out["agg"] = new - px[red]
        px[red] = new
    # Back-off side: ceil like the aggressive side (floor gave 0 for every
    # frac < 1 at max_backoff 1), then limited by reward loss below the reference.
    back = int(math.ceil(f * p.max_backoff - 1e-9)) if p.max_backoff > 0 else 0
    while back > 0:
        below = max(0, int(ref[add]) - (px[add] - back))
        loss = 1.0 - float(df) ** below if below > 0 else 0.0
        if loss <= p.max_reward_loss + 1e-12 and px[add] - back >= 1:
            out["reward_loss"] = round(loss, 6)
            break
        back -= 1
    out["back"] = back
    px[add] -= back
    out["yes_cents"], out["no_cents"] = px["yes"], px["no"]
    return out
