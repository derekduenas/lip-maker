"""Paper gates for the APEX Kalshi maker (COMMAND decision 2026-10-04, Derek approved).

* Oct 6 2026 is a CHECKPOINT only (diagnostic, not a go/no-go):
  - >= 30 real (non-synthetic) Kalshi paper fills. "Real" means a public
    trade print reached our queue in the fill simulator (source "print").
    Fills inferred from our book being crossed (``paper_cross``) and
    synthetic fills are reported separately and do not count.
  - clock_skew quote pulls < 100/day: the trailing 24 h once the session is
    24 h old, else the session count scaled to a day.
  - 5-minute markout reported (non-synthetic Kalshi fills with a measured
    300 s markout). Reported, not thresholded.
* Oct 10 2026 is the real go/no-go. Per series, mm.session_gates.series_go
  (>= 5 days, >= 30 settled fills, net > 0, 5-min markout per fill < reward
  per fill, still > 0 after a 50% reward haircut), plus the advisory
  tools/readiness_report.py. Going live stays an explicit human decision;
  nothing here arms anything.

Env overrides: LIP_CHECKPOINT_DATE, LIP_CHECKPOINT_MIN_REAL_FILLS,
LIP_CHECKPOINT_MAX_SKEW_PULLS_DAY, LIP_GO_NO_GO_DATE.
"""
from __future__ import annotations

import os

CHECKPOINT_DATE = "2026-10-06"
CHECKPOINT_MIN_REAL_KALSHI_FILLS = 30
CHECKPOINT_MAX_CLOCK_SKEW_PULLS_PER_DAY = 100
CHECKPOINT_MARKOUT_HORIZON_S = 300
GO_NO_GO_DATE = "2026-10-10"
GO_NO_GO_CRITERIA = (
    "per series mm.session_gates.series_go: >= 5 days, >= 30 settled fills, net > 0, "
    "5-min markout per fill < reward per fill, > 0 after a 50% reward haircut; "
    "plus tools/readiness_report.py (advisory). Live arming stays a human decision."
)


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def gate_config() -> dict:
    return {
        "checkpoint_date": os.environ.get("LIP_CHECKPOINT_DATE") or CHECKPOINT_DATE,
        "min_real_kalshi_fills": int(_num("LIP_CHECKPOINT_MIN_REAL_FILLS", CHECKPOINT_MIN_REAL_KALSHI_FILLS)),
        "max_clock_skew_pulls_per_day": _num("LIP_CHECKPOINT_MAX_SKEW_PULLS_DAY",
                                             CHECKPOINT_MAX_CLOCK_SKEW_PULLS_PER_DAY),
        "markout_horizon_s": CHECKPOINT_MARKOUT_HORIZON_S,
        "go_no_go_date": os.environ.get("LIP_GO_NO_GO_DATE") or GO_NO_GO_DATE,
        "go_no_go_criteria": GO_NO_GO_CRITERIA,
    }


def skew_pulls_per_day(*, pulls_24h: int, pulls_session: int, session_s: float) -> float | None:
    """Trailing 24 h once the session is a day old, else the session rate."""
    if session_s >= 86400.0:
        return float(pulls_24h)
    if session_s < 600.0:
        return None  # too short to rate
    return float(pulls_session) * 86400.0 / float(session_s)


def checkpoint_report(*, kalshi_fills_print: int, kalshi_fills_cross: int, kalshi_fills_synthetic: int,
                      kalshi_fills_total: int, sample_fills: int, skew_pulls_24h: int,
                      skew_pulls_session: int, session_s: float, markout_5m_usd: float,
                      markout_5m_fills: int, markout_5m_contracts: float, now: float | None = None) -> dict:
    """Status ``checkpoint``: the Oct 6 numbers and their PASS/FAIL/PENDING."""
    cfg = gate_config()
    rate = skew_pulls_per_day(pulls_24h=skew_pulls_24h, pulls_session=skew_pulls_session, session_s=session_s)
    real = int(kalshi_fills_print)
    fills_ok = real >= cfg["min_real_kalshi_fills"]
    skew_ok = None if rate is None else rate < cfg["max_clock_skew_pulls_per_day"]
    markout_ok = markout_5m_fills > 0
    per_c = (markout_5m_usd * 100.0 / markout_5m_contracts) if markout_5m_contracts > 0 else None
    checks = {
        "real_kalshi_fills": {"value": real, "min": cfg["min_real_kalshi_fills"],
                              "status": "PASS" if fills_ok else "PENDING"},
        "clock_skew_pulls_per_day": {"value": None if rate is None else round(rate, 1),
                                     "max": cfg["max_clock_skew_pulls_per_day"],
                                     "status": ("PENDING" if skew_ok is None else ("PASS" if skew_ok else "FAIL"))},
        "markout_5m_reported": {"fills": int(markout_5m_fills),
                                "status": "PASS" if markout_ok else "PENDING"},
    }
    overall = ("PASS" if all(c["status"] == "PASS" for c in checks.values())
               else ("FAIL" if any(c["status"] == "FAIL" for c in checks.values()) else "PENDING"))
    return {
        "checkpoint_date": cfg["checkpoint_date"],
        "go_no_go_date": cfg["go_no_go_date"],
        "go_no_go_criteria": cfg["go_no_go_criteria"],
        "kind": "checkpoint (diagnostic, not go/no-go)",
        "overall": overall,
        "checks": checks,
        "kalshi_fills": {"real_print": real, "paper_cross": int(kalshi_fills_cross),
                         "synthetic": int(kalshi_fills_synthetic), "total": int(kalshi_fills_total),
                         "from_sampling_group": int(sample_fills)},
        "clock_skew_pulls": {"last_24h": int(skew_pulls_24h), "session": int(skew_pulls_session),
                             "session_hours": round(session_s / 3600.0, 2)},
        "markout_5m": {"fills": int(markout_5m_fills), "contracts": round(markout_5m_contracts, 4),
                       "usd": round(markout_5m_usd, 4),
                       "cents_per_contract": None if per_c is None else round(per_c, 3)},
    }
