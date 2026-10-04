"""Statistical bar for the Oct 10 go/no-go (paper only, advisory).

``mm.session_gates.series_go`` asks per series for >= 30 settled fills, net > 0
and a markout cost under the reward per fill. It has no notion of sampling
noise: 30 fills of a ~4c-per-contract-SD markout cannot tell a +0.5c edge from
zero (about 400 independent fills are needed at 80% power; ~940 at a t > 3
hurdle). This module adds what a pre-registered evaluation needs:

* the 5-minute markout is aggregated PER EVENT (fills in one event are one
  correlated draw: ten fills in KXCPI-26OCT30 are one observation, not ten);
* a one-sided lower confidence bound on the mean (Student-t, df = events - 1);
* the net edge per contract = markout + (1 - haircut) x reward per contract,
  and the verdict GO only when that LOWER bound is > 0 on enough events;
* how many more independent events are needed for the planned edge at 80%
  power, from the observed spread (a planning number, not a verdict);
* a fingerprint of the tuning parameters, so a change during the evaluation
  window is visible (the frozen window restarts) and every distinct set tried
  is counted (trials, for multiple-testing discipline).

Everything here is a pure function of numbers; the engine feeds it.
Markout sign: cents per contract, positive = the mid moved in our favour.
"""
from __future__ import annotations

import hashlib
import math
import os

# One-sided 90% Student-t critical values by degrees of freedom (1..30).
_T90 = (3.078, 1.886, 1.638, 1.533, 1.476, 1.440, 1.415, 1.397, 1.383, 1.372,
        1.363, 1.356, 1.350, 1.345, 1.341, 1.337, 1.333, 1.330, 1.328, 1.325,
        1.323, 1.321, 1.319, 1.318, 1.316, 1.315, 1.314, 1.313, 1.311, 1.310)
Z_ALPHA_ONE_SIDED_5 = 1.645   # power calculation (alpha 5%)
Z_POWER_80 = 0.84

# Env names matching these are never hashed or stored (URLs/keys/topics).
_SECRET_MARKERS = ("WEBHOOK", "KEY", "TOKEN", "SECRET", "NTFY", "PASSWORD", "URL")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def t_crit_90(df: int) -> float:
    """One-sided 90% critical value of Student-t with ``df`` degrees of freedom."""
    df = int(df)
    if df < 1:
        return float("inf")
    if df <= 30:
        return _T90[df - 1]
    # Beyond the table, the value at the START of each band (t falls with
    # df), so the bound never gets looser than the exact one.
    if df <= 60:
        return 1.309   # df 31
    if df <= 120:
        return 1.296   # df 61
    if df <= 240:
        return 1.289   # df 121
    if df < 1000:
        return 1.285   # df 241
    return 1.282       # normal limit (exact 1.2824 at df 1000)


def config() -> dict:
    """Pre-registered thresholds (env-overridable; defaults are the plan)."""
    return {"min_events": int(_num("LIP_GO_MIN_EVENTS", 30)),
            "target_edge_cents": _num("LIP_GO_TARGET_EDGE_CENTS", 0.5),
            "reward_haircut": min(1.0, max(0.0, _num("LIP_GO_REWARD_HAIRCUT", 0.5))),
            "min_frozen_days": _num("LIP_GO_MIN_FROZEN_DAYS", 3.0)}


def event_stats(events: dict) -> dict:
    """Mean/SD/SE/lower bound of the 5-minute markout across events.

    ``events``: event -> [fills, contracts, usd] (usd = sum of the fills'
    5-minute markouts in dollars). Each event contributes its contract-weighted
    mean in cents per contract; events are then equally weighted (the unit of
    independence). Events with no contracts are ignored."""
    means = [row[2] * 100.0 / row[1] for row in events.values() if row[1] > 0]
    k = len(means)
    fills = int(sum(row[0] for row in events.values() if row[1] > 0))
    if k == 0:
        return {"events": 0, "fills": 0, "mean_cents": None, "sd_cents": None,
                "se_cents": None, "lower_90_cents": None}
    mean = sum(means) / k
    if k < 2:
        return {"events": k, "fills": fills, "mean_cents": mean, "sd_cents": None,
                "se_cents": None, "lower_90_cents": None}
    var = sum((m - mean) ** 2 for m in means) / (k - 1)
    sd = math.sqrt(var)
    se = sd / math.sqrt(k)
    return {"events": k, "fills": fills, "mean_cents": mean, "sd_cents": sd, "se_cents": se,
            "lower_90_cents": mean - t_crit_90(k - 1) * se}


def events_needed(sd_cents: float | None, target_edge_cents: float) -> int | None:
    """Independent events for 80% power at one-sided alpha 5% to detect
    ``target_edge_cents`` given the observed event-level SD (planning number)."""
    if not sd_cents or target_edge_cents <= 0:
        return None
    return int(math.ceil(((Z_ALPHA_ONE_SIDED_5 + Z_POWER_80) * sd_cents / target_edge_cents) ** 2))


def verdict(stats: dict, *, reward_cents_per_contract: float, frozen_days: float,
            cfg: dict | None = None) -> dict:
    """GO / NO_GO / INSUFFICIENT with the reason, from ``event_stats``.

    edge per contract = markout + (1 - haircut) x reward per contract (the
    reward is an estimate, never paid money, hence the haircut). GO needs the
    one-sided 90% LOWER bound of that edge > 0 on >= min_events independent
    events and a parameter set unchanged for >= min_frozen_days. NO_GO when
    the UPPER side cannot reach 0 either (mean + t x SE <= 0) on enough
    events; otherwise INSUFFICIENT (keep collecting; ``events_needed`` says
    how much)."""
    cfg = cfg or config()
    keep = 1.0 - cfg["reward_haircut"]
    reward = keep * float(reward_cents_per_contract or 0.0)
    out = {"criteria": cfg, "reward_cents_per_contract_after_haircut": round(reward, 4),
           "frozen_days": round(float(frozen_days), 3)}
    k, mean, se = stats["events"], stats["mean_cents"], stats["se_cents"]
    out["events_needed_for_target"] = events_needed(stats["sd_cents"], cfg["target_edge_cents"])
    if mean is None or se is None:
        return dict(out, verdict="INSUFFICIENT", why="too_few_events", edge_lower_90_cents=None,
                    edge_mean_cents=None if mean is None else round(mean + reward, 4))
    edge_mean = mean + reward
    t = t_crit_90(k - 1)
    lower, upper = edge_mean - t * se, edge_mean + t * se
    out.update({"edge_mean_cents": round(edge_mean, 4), "edge_lower_90_cents": round(lower, 4),
                "edge_upper_90_cents": round(upper, 4)})
    if k < cfg["min_events"]:
        return dict(out, verdict="INSUFFICIENT", why="events")
    if frozen_days < cfg["min_frozen_days"]:
        return dict(out, verdict="INSUFFICIENT", why="parameters_not_frozen")
    if lower > 0:
        return dict(out, verdict="GO", why="lower_bound_positive")
    if upper <= 0:
        return dict(out, verdict="NO_GO", why="upper_bound_not_positive")
    return dict(out, verdict="INSUFFICIENT", why="interval_spans_zero")


def fingerprint(environ: dict | None = None) -> tuple[str, dict]:
    """(sha256 prefix, {name: value}) of the LIP_* tuning parameters.

    Names that look like secrets or endpoints are excluded. Stable under
    ordering. It covers env policy only, not code: deploy the code version
    separately (git commit) when freezing an evaluation."""
    env = os.environ if environ is None else environ
    params = {k: str(v) for k, v in sorted(env.items())
              if k.startswith("LIP_") and not any(m in k for m in _SECRET_MARKERS)}
    blob = "\n".join(f"{k}={v}" for k, v in params.items()).encode()
    return hashlib.sha256(blob).hexdigest()[:16], params


def note_params(history: list, now: float, environ: dict | None = None) -> bool:
    """Append {fp, ts} to ``history`` when the fingerprint changed. True if it did."""
    fp, _params = fingerprint(environ)
    if history and history[-1].get("fp") == fp:
        return False
    history.append({"fp": fp, "ts": float(now)})
    del history[:-200]
    return True


def frozen_days(history: list, now: float) -> float:
    """Days since the last parameter change (0 when nothing was recorded)."""
    if not history:
        return 0.0
    return max(0.0, (float(now) - float(history[-1]["ts"])) / 86400.0)
