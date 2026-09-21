"""Entry cutoff policies for the paper experiment. FROZEN 2026-09-21.

What a cutoff is
----------------
How close to MARKET CLOSE we stop opening new reward-driven quotes. It is a
settlement-risk control and it reads the settlement clock only
(engine.market_clock). Reward expiry is a separate clock and stops
reward-driven entry on its own; neither of them may disable inventory
management, which runs until a position is flat.

The policies
------------
Exact formulas, stated before any evaluation. `duration_min` is the
market's own trading window from the venue, (close_time - open_time)/60.

    control_30min        cutoff_min = 30.0
                         The existing live risk control, UNCHANGED. This is
                         the control arm and also what live continues to use.

    fixed_60s            cutoff_min = 1.0

    proportional_20pct   cutoff_min = min(MAX_MIN, max(MIN_MIN,
                                          PROPORTION * duration_min))
                         PROPORTION = 0.20
                         MIN_MIN    = 1.0   minutes (60 seconds)
                         MAX_MIN    = 30.0  minutes

MAX_MIN equals the control so no variant is ever MORE conservative than
today's setting; MIN_MIN keeps a floor under the very short markets.

When `duration_min` is unknown the policy returns the control value. An
unknown denominator must not silently become a permissive cutoff.

Pre-registration
----------------
These constants are frozen before the matched comparison is run, and
`policy_fingerprint()` is recorded with the results. Choosing a cutoff after
seeing which one won would make the comparison meaningless, so the winner is
not selectable from here: the evaluation reports every arm.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

CONTROL_CUTOFF_MIN = 30.0
FIXED_60S_CUTOFF_MIN = 1.0
PROPORTION = 0.20
MIN_MIN = 1.0
MAX_MIN = 30.0

POLICY_CONTROL = "control_30min"
POLICY_FIXED_60S = "fixed_60s"
POLICY_PROPORTIONAL = "proportional_20pct"

ALL_POLICIES = (POLICY_CONTROL, POLICY_FIXED_60S, POLICY_PROPORTIONAL)


@dataclass(frozen=True)
class CutoffDecision:
    policy: str
    cutoff_min: float
    basis: str          # how the number was produced
    duration_min: Optional[float] = None


def cutoff_minutes(policy: str, duration_min: Optional[float] = None
                   ) -> CutoffDecision:
    """Minutes before market close at which new entries stop."""
    if policy == POLICY_CONTROL:
        return CutoffDecision(policy, CONTROL_CUTOFF_MIN,
                              "constant (unchanged live control)", duration_min)
    if policy == POLICY_FIXED_60S:
        return CutoffDecision(policy, FIXED_60S_CUTOFF_MIN,
                              "constant 60 seconds", duration_min)
    if policy == POLICY_PROPORTIONAL:
        if duration_min is None or duration_min <= 0:
            return CutoffDecision(policy, CONTROL_CUTOFF_MIN,
                                  "duration unknown -> fell back to control",
                                  duration_min)
        raw = PROPORTION * float(duration_min)
        val = min(MAX_MIN, max(MIN_MIN, raw))
        return CutoffDecision(
            policy, val,
            f"clamp({PROPORTION}*{duration_min:.2f}={raw:.2f}, "
            f"{MIN_MIN}, {MAX_MIN})", duration_min)
    raise ValueError(f"unknown cutoff policy {policy!r}")


def policy_fingerprint() -> str:
    """Hash of the frozen constants, recorded alongside any result so a
    later reader can tell whether the policy was edited after the fact."""
    blob = json.dumps({
        "control": CONTROL_CUTOFF_MIN, "fixed_60s": FIXED_60S_CUTOFF_MIN,
        "proportion": PROPORTION, "min_min": MIN_MIN, "max_min": MAX_MIN,
        "policies": list(ALL_POLICIES)}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def describe() -> dict:
    return {
        "fingerprint": policy_fingerprint(),
        "frozen_utc": "2026-09-21",
        "policies": {
            POLICY_CONTROL: f"cutoff_min = {CONTROL_CUTOFF_MIN}",
            POLICY_FIXED_60S: f"cutoff_min = {FIXED_60S_CUTOFF_MIN}",
            POLICY_PROPORTIONAL: (
                f"cutoff_min = min({MAX_MIN}, max({MIN_MIN}, "
                f"{PROPORTION} * duration_min)); duration_min = "
                "(close_time - open_time)/60 from the venue; "
                "unknown duration -> control"),
        },
        "clock": "market close only; reward expiry is a separate clock",
        "inventory": "exit management runs regardless of any cutoff",
    }
