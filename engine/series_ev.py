"""Pre-trade EV check (#103) — refuse markets where 7d adverse > rebate.

DIAGNOSIS (Apr 27 audit):
  Toxicity filter is reactive — it waits for fills to accumulate before
  blacklisting. By then we've already eaten the loss. KXCOFFEEW lost
  -$71.61 settlement vs $10 rebate before any per-market detector fired.

THIS MODULE: compute per-series 7d EV from settlement_log + Kalshi-paid
rebate ledger, and reject any market in a series whose adverse cost
exceeds rebate captured. Acts as a PROACTIVE gate before quote placement.

CACHE: per-series EV is recomputed every CACHE_TTL_SEC (default 1h) since
settlement data only changes when markets close.

Series defaults to ALLOW when:
  - No settlement history (innocent until proven guilty)
  - Fewer than MIN_SETTLEMENTS data points (sample too thin)
  - Series is in SIZE_MULTIPLIER_BY_SERIES winner table (overrides EV check)
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from config import settings

_log = logging.getLogger(__name__)

CACHE_TTL_SEC = 3600        # 1 hour — settlement data is hourly-fresh at most
MIN_SETTLEMENTS = 3         # need at least this many to trust the verdict
EV_RATIO_FLOOR = 0.5        # rebate must cover ≥ this fraction of adverse cost

_cache: dict[str, tuple[dict, float]] = {}   # series → (verdict, ts)


def _compute_series_ev(series_prefix: str, db_path: str) -> dict:
    """Compute 7d settlement-based EV for one series.

    Returns dict with: n_settlements, total_adverse, total_rebate, ratio,
    verdict ('allow' | 'block_negative_ev' | 'thin_data' | 'whitelist_winner').
    """
    # Whitelist override — winner-tier series always allowed regardless of
    # what the recent settlement data says. They've earned the trust via
    # Apr 27 audit (KXBRENTD/CORNW/COPPERD/GOLDW/COCOAW).
    if series_prefix in settings.SIZE_MULTIPLIER_BY_SERIES:
        return {
            "series": series_prefix,
            "verdict": "whitelist_winner",
            "ratio": None, "n_settlements": None,
            "total_adverse": None, "total_rebate": None,
        }

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(CASE WHEN our_realized_usd < 0
                                           THEN our_realized_usd ELSE 0 END), 0) AS adverse,
                          COALESCE(SUM(rebate_earned_usd), 0) AS rebate
                   FROM settlement_log
                   WHERE series_prefix = ?
                     AND datetime(close_time) > datetime(?)""",
                (series_prefix, cutoff),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        _log.warning(f"series_ev query failed for {series_prefix}: {e}")
        return {"series": series_prefix, "verdict": "allow",
                "ratio": None, "n_settlements": None,
                "total_adverse": None, "total_rebate": None}

    n_settlements = int(row[0] or 0)
    total_adverse = abs(float(row[1] or 0.0))   # adverse is sum of negatives → make positive
    total_rebate = float(row[2] or 0.0)

    # AUDIT FIX (2026-04-28): settlement_reconciler.py:205 currently
    # hardcodes rebate_earned_usd=0 (data pipeline not wired yet). Without
    # this guard, EVERY non-whitelisted series with ≥3 settlements + any
    # loss would block — system-wide kill switch. Treat zero-rebate +
    # any-settlements as DATA-GAP, not negative-EV. Re-enable strict
    # check once settlement_reconciler populates real rebate values.
    DATA_PIPELINE_WIRED = False  # flip to True when reconciler writes rebates
    if total_rebate == 0 and n_settlements > 0 and not DATA_PIPELINE_WIRED:
        return {
            "series": series_prefix, "verdict": "data_gap",
            "ratio": None, "n_settlements": n_settlements,
            "total_adverse": round(total_adverse, 2), "total_rebate": 0.0,
        }

    if n_settlements < MIN_SETTLEMENTS:
        verdict = "thin_data"
    else:
        if total_adverse == 0:
            ratio = float("inf")
        else:
            ratio = total_rebate / total_adverse
        verdict = "allow" if ratio >= EV_RATIO_FLOOR else "block_negative_ev"

    ratio_val = (total_rebate / total_adverse) if total_adverse > 0 else None
    return {
        "series":         series_prefix,
        "verdict":        verdict,
        "ratio":          round(ratio_val, 3) if ratio_val is not None else None,
        "n_settlements":  n_settlements,
        "total_adverse":  round(total_adverse, 2),
        "total_rebate":   round(total_rebate, 2),
    }


def check_series_ev(series_prefix: str, db_path: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Cached per-series for CACHE_TTL_SEC.

    `reason` is empty string when allowed; populated with explanation when
    blocked, suitable for logging in safety-gate output.
    """
    if not series_prefix:
        return True, ""
    now = time.time()
    cached = _cache.get(series_prefix)
    if cached and now - cached[1] < CACHE_TTL_SEC:
        verdict_dict = cached[0]
    else:
        verdict_dict = _compute_series_ev(series_prefix, db_path)
        _cache[series_prefix] = (verdict_dict, now)

    v = verdict_dict["verdict"]
    if v in ("allow", "thin_data", "whitelist_winner", "data_gap"):
        return True, ""
    if v == "block_negative_ev":
        return False, (f"ev_check[{series_prefix}] adverse=${verdict_dict['total_adverse']} "
                       f"rebate=${verdict_dict['total_rebate']} ratio={verdict_dict['ratio']} "
                       f"< {EV_RATIO_FLOOR} (n={verdict_dict['n_settlements']})")
    return True, ""  # unknown verdict → fail-open


def reset_cache() -> None:
    """For tests."""
    _cache.clear()
