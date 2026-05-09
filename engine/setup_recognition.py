"""Setup Recognition Engine — learn what KIND of market pays, not which specific ones.

The thesis (per user 2026-05-03): the system should learn SETUP PATTERNS — the
characteristics that correlate with actual paid rebate — not memorize specific
tickers. New markets matching a known-paying SETUP should rank high regardless
of series; new markets with familiar bad setups should rank low even if their
series has paid before.

DATA FLOW:
  1. setup_recorder.py runs nightly. For every market that settled with a known
     outcome (rebate_earned_usd from settlement_log + Kalshi paid_out flag),
     compute its feature vector and write to setup_outcomes.
  2. capital_allocator.py calls setup_quality_score(features) for each candidate.
     Returns a multiplier (0.5–2.0) based on K-nearest historical setups.
  3. Multiplier scales expected_net_per_day. Markets with PROVEN-PAYING setups
     rise to the top organically. No hardcoded series lists.

FEATURES (per market):
  pool_per_day, target_size, discount_factor, hours_to_settle (at quote time),
  observed_competition, cadence_class (DAILY/WEEKLY/MONTHLY/EVENT)

DISTANCE: Normalized Euclidean on log-scale where features are skewed (pool,
target, comp), linear on bounded (DF), categorical for cadence.

OUTCOME: rebate_paid_usd / period_days = paid_per_day. Used both as similarity
target and as score normalizer (typical_paid → 1.0 multiplier).
"""
from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


# ── Schema ────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_outcomes (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  market_ticker            TEXT NOT NULL UNIQUE,
  series_ticker            TEXT,
  -- Feature vector (raw; normalization happens at distance compute time)
  pool_per_day_usd         REAL NOT NULL,
  target_size              REAL NOT NULL,
  discount_factor          REAL NOT NULL,
  hours_to_settle_at_record REAL,
  observed_competition     REAL,
  cadence_class            TEXT,
  -- Outcome
  rebate_paid_usd          REAL NOT NULL,
  realized_pnl_usd         REAL,
  net_outcome_usd          REAL,
  period_days              REAL,
  paid_per_day_usd         REAL,
  -- Metadata
  recorded_at              TEXT NOT NULL,
  settlement_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_setup_series ON setup_outcomes(series_ticker);
CREATE INDEX IF NOT EXISTS idx_setup_paid ON setup_outcomes(paid_per_day_usd);
CREATE INDEX IF NOT EXISTS idx_setup_recorded ON setup_outcomes(recorded_at);
"""


# ── Tunables ──────────────────────────────────────────────────────────────
K_NEIGHBORS = 7
# Distance scale factors (denominators in normalization). Tuned for typical ranges.
LOG_SCALE = 5.0
# Multiplier bounds (don't let one outlier swing too hard)
SCORE_MIN, SCORE_MAX = 0.5, 2.0
# Min outcomes in DB before we trust the score (cold-start)
MIN_OUTCOMES_FOR_SCORE = 20


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ── Cadence classifier ────────────────────────────────────────────────────

def classify_cadence(hours_to_settle: float) -> str:
    """Bucket markets by typical settlement period."""
    if hours_to_settle <= 36:
        return "DAILY"
    if hours_to_settle <= 192:
        return "WEEKLY"
    if hours_to_settle <= 720:
        return "MONTHLY"
    return "LONG"


# ── Feature vector ────────────────────────────────────────────────────────

def features_from_market(
    pool_per_day: float, target_size: float, discount_factor: float,
    hours_to_settle: float, observed_competition: float,
) -> dict:
    """Build canonical feature vector for one market candidate."""
    return {
        "pool":     max(0.01, float(pool_per_day or 0)),
        "target":   max(1.0, float(target_size or 1)),
        "df":       max(0.0, min(1.0, float(discount_factor or 0.5))),
        "hours":    max(0.5, float(hours_to_settle or 24)),
        "comp":     max(0.0, float(observed_competition or 0)),
        "cadence":  classify_cadence(hours_to_settle),
    }


def feature_distance(a: dict, b: dict) -> float:
    """Normalized Euclidean distance between two setups."""
    log_pool   = (math.log(a["pool"] + 1) - math.log(b["pool"] + 1)) / LOG_SCALE
    log_target = (math.log(a["target"] + 1) - math.log(b["target"] + 1)) / LOG_SCALE
    df_diff    = a["df"] - b["df"]
    log_comp   = (math.log(a["comp"] + 1) - math.log(b["comp"] + 1)) / LOG_SCALE
    cadence_diff = 0.0 if a["cadence"] == b["cadence"] else 0.4
    return math.sqrt(
        log_pool * log_pool
        + log_target * log_target
        + df_diff * df_diff
        + log_comp * log_comp
        + cadence_diff * cadence_diff
    )


# ── Score calculation ─────────────────────────────────────────────────────

_outcomes_cache: list[dict] = []
_cache_loaded_at: float = 0.0
_CACHE_TTL_SEC = 300


def _load_outcomes(db_path: str = settings.DB_PATH) -> list[dict]:
    """Load all setup outcomes (cached 5 min). Filters to net-positive setups
    so we don't pollute K-nearest with proven losers (those become DEMOTERS,
    handled separately via auto_calibrate)."""
    global _outcomes_cache, _cache_loaded_at
    import time
    if _outcomes_cache and time.time() - _cache_loaded_at < _CACHE_TTL_SEC:
        return _outcomes_cache
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        rows = conn.execute("""
            SELECT market_ticker, series_ticker, pool_per_day_usd, target_size,
                   discount_factor, hours_to_settle_at_record, observed_competition,
                   cadence_class, paid_per_day_usd
            FROM setup_outcomes
            WHERE paid_per_day_usd >= 0  -- include zeros so the model knows what doesn't pay
        """).fetchall()
        conn.close()
        _outcomes_cache = [
            {
                "ticker":  r[0],
                "series":  r[1],
                "features": {
                    "pool":    float(r[2]),
                    "target":  float(r[3]),
                    "df":      float(r[4]),
                    "hours":   float(r[5] or 24),
                    "comp":    float(r[6] or 0),
                    "cadence": r[7] or "UNKNOWN",
                },
                "paid_per_day": float(r[8] or 0),
            }
            for r in rows
        ]
        _cache_loaded_at = time.time()
    except Exception:
        _outcomes_cache = []
    return _outcomes_cache


def setup_quality_score(features: dict, db_path: str = settings.DB_PATH) -> dict:
    """For a candidate's feature vector, return a setup quality multiplier.

    Algorithm:
      1. Find K nearest historical outcomes.
      2. Distance-weighted average of paid_per_day_usd among neighbors.
      3. Score = weighted_avg / median_paid (so 1.0 = typical, 2.0 = top-tier).
      4. Clamp to [SCORE_MIN, SCORE_MAX].

    Returns dict with score + diagnostic fields.
    """
    outcomes = _load_outcomes(db_path)
    if len(outcomes) < MIN_OUTCOMES_FOR_SCORE:
        return {
            "score":          1.0,
            "neighbors":      0,
            "neighbor_avg":   None,
            "confidence":     "cold_start",
        }
    # K-nearest by feature distance
    scored = [(o, feature_distance(features, o["features"])) for o in outcomes]
    scored.sort(key=lambda x: x[1])
    k = min(K_NEIGHBORS, len(scored))
    neighbors = scored[:k]
    # Inverse-distance weighting (avoid divide-by-zero with +0.05)
    weights = [1.0 / (d + 0.05) for _, d in neighbors]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        return {"score": 1.0, "neighbors": k, "neighbor_avg": None, "confidence": "weight_zero"}
    weighted_avg = sum(o["paid_per_day"] * w for (o, _), w in zip(neighbors, weights)) / weight_sum
    # Normalize against population median for "typical = 1.0"
    paids = sorted(o["paid_per_day"] for o in outcomes)
    median_paid = paids[len(paids) // 2] if paids else 0
    if median_paid <= 0.001:
        # Cold dataset: just return a small multiplier proportional to absolute paid
        score = max(SCORE_MIN, min(SCORE_MAX, 0.5 + weighted_avg))
    else:
        score = max(SCORE_MIN, min(SCORE_MAX, weighted_avg / median_paid))
    return {
        "score":          round(score, 3),
        "neighbors":      k,
        "neighbor_avg":   round(weighted_avg, 4),
        "median_paid":    round(median_paid, 4),
        "confidence":     "ok",
        "top_neighbor":   neighbors[0][0]["ticker"][:30] if neighbors else None,
    }


def record_outcome(
    market_ticker: str,
    series_ticker: str,
    pool_per_day: float,
    target_size: float,
    discount_factor: float,
    hours_to_settle_at_record: float,
    observed_competition: float,
    rebate_paid_usd: float,
    realized_pnl_usd: float,
    settlement_at: str,
    period_days: float = 1.0,
    db_path: str = settings.DB_PATH,
) -> None:
    """Persist a settled market's setup + outcome. Idempotent on market_ticker."""
    ensure_schema(db_path)
    paid_per_day = (rebate_paid_usd or 0) / max(0.04, period_days)
    net = (rebate_paid_usd or 0) + (realized_pnl_usd or 0)
    cadence = classify_cadence(hours_to_settle_at_record or 24)
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO setup_outcomes
               (market_ticker, series_ticker, pool_per_day_usd, target_size,
                discount_factor, hours_to_settle_at_record, observed_competition,
                cadence_class, rebate_paid_usd, realized_pnl_usd, net_outcome_usd,
                period_days, paid_per_day_usd, recorded_at, settlement_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (market_ticker, series_ticker, pool_per_day, target_size,
             discount_factor, hours_to_settle_at_record, observed_competition,
             cadence, rebate_paid_usd, realized_pnl_usd, net, period_days,
             paid_per_day, now_iso, settlement_at),
        )
        conn.commit()
    finally:
        conn.close()
