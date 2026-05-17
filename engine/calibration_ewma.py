"""Per-market calibration EWMA (Track A.5).

The yield equation in `cross_venue/yield_equation.py` multiplies the
theoretical pool × share × qualify_prob × time_factor by a single
calibration constant (KALSHI_CALIB=0.25, PM_CALIB=0.10). Those are
back-of-envelope priors. In reality, the actual paid / projected ratio
varies a lot per market — weather dailies pay ~0.18, crypto monthlies
sometimes >0.35, SIG-dominated sports near 0.05.

This module learns those ratios online and serves them via
`calib_for(key, fallback)`. EWMA with α=0.05 means each settled market
moves the estimate ~5% toward the latest observation — slow enough to
ride through noise, fast enough to track regime shifts within ~20
settlements.

KEY VS GRANULARITY
------------------
We key calibration by **series prefix** (e.g. "KXBRENTD") rather than
full ticker. Per-ticker is too sparse (most markets settle once) while
per-series captures the SIG-vs-niche character of a contract family.

SCHEMA
------
    market_calibration(
        key TEXT PRIMARY KEY,
        calibration REAL NOT NULL,
        n_samples INTEGER NOT NULL,
        last_ratio REAL,                 -- most recent observation
        last_predicted_usd REAL,
        last_actual_usd REAL,
        updated_at TEXT
    )

USAGE
-----
    from engine.calibration_ewma import calib_for, update
    c = calib_for("KXBRENTD", fallback=0.25)              # → learned or 0.25
    update("KXBRENTD", predicted_usd=12.0, actual_usd=2.8) # ratio 0.233 → EWMA
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)

DEFAULT_ALPHA = 0.05
RATIO_CLAMP_MAX = 5.0   # cap a single noisy observation
RATIO_CLAMP_MIN = 0.0   # negative payments aren't a calibration signal
MIN_PREDICTED_USD = 0.50  # below this, signal/noise is bad; skip update


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS market_calibration (
    key                 TEXT PRIMARY KEY,
    calibration         REAL NOT NULL,
    n_samples           INTEGER NOT NULL DEFAULT 0,
    last_ratio          REAL,
    last_predicted_usd  REAL,
    last_actual_usd     REAL,
    updated_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_market_calibration_updated
    ON market_calibration(updated_at);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def series_prefix(ticker_or_key: str) -> str:
    """Extract series prefix from a Kalshi ticker. Pass-through for keys
    that already are prefixes."""
    return ticker_or_key.split("-", 1)[0] if "-" in ticker_or_key else ticker_or_key


def update(key: str, predicted_usd: float, actual_usd: float,
           *, alpha: float = DEFAULT_ALPHA,
           db_path: str = settings.DB_PATH) -> Optional[float]:
    """EWMA-update calibration for `key` from one settlement observation.

    Args:
        key: series prefix or ticker (will be normalized to prefix).
        predicted_usd: model-projected rebate for this market.
        actual_usd: actual paid rebate (from settlement_log.rebate_earned_usd).
        alpha: EWMA smoothing factor (0.05 = slow, 0.5 = fast).

    Returns:
        New calibration value, or None if the observation was skipped
        (predicted_usd too small to be a useful signal).
    """
    if predicted_usd is None or predicted_usd < MIN_PREDICTED_USD:
        return None
    if actual_usd is None:
        return None
    actual_usd = max(0.0, float(actual_usd))
    raw_ratio = actual_usd / predicted_usd
    ratio = max(RATIO_CLAMP_MIN, min(RATIO_CLAMP_MAX, raw_ratio))

    k = series_prefix(key)
    ensure_schema(db_path)
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            row = conn.execute(
                "SELECT calibration, n_samples FROM market_calibration WHERE key=?",
                (k,),
            ).fetchone()
            if row is None:
                # Seed at the observation itself; the EWMA will smooth subsequent updates
                new_calib = ratio
                new_n = 1
            else:
                old_calib, old_n = float(row[0]), int(row[1])
                new_calib = alpha * ratio + (1.0 - alpha) * old_calib
                new_n = old_n + 1
            conn.execute(
                """INSERT INTO market_calibration
                   (key, calibration, n_samples, last_ratio,
                    last_predicted_usd, last_actual_usd, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       calibration=excluded.calibration,
                       n_samples=excluded.n_samples,
                       last_ratio=excluded.last_ratio,
                       last_predicted_usd=excluded.last_predicted_usd,
                       last_actual_usd=excluded.last_actual_usd,
                       updated_at=excluded.updated_at""",
                (k, new_calib, new_n, ratio,
                 float(predicted_usd), actual_usd,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return new_calib
        finally:
            conn.close()
    except Exception as e:
        _log.warning(f"calibration_ewma.update[{k}] failed: {e}")
        return None


def calib_for(key: str, fallback: float,
              *, min_samples: int = 5,
              db_path: str = settings.DB_PATH) -> float:
    """Read the learned calibration for `key`. Returns fallback when:
      - PER_MARKET_CALIB_ENABLED is False (feature flag)
      - no row exists yet
      - n_samples is below min_samples (cold start)
    """
    if not getattr(settings, "PER_MARKET_CALIB_ENABLED", False):
        return float(fallback)
    k = series_prefix(key)
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT calibration, n_samples FROM market_calibration WHERE key=?",
                (k,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return float(fallback)
    if row is None or int(row[1]) < min_samples:
        return float(fallback)
    return float(row[0])


def calib_dict(db_path: str = settings.DB_PATH) -> dict[str, dict]:
    """Bulk-read all learned calibrations. For dashboards / reports."""
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            rows = conn.execute(
                """SELECT key, calibration, n_samples, last_ratio,
                          last_predicted_usd, last_actual_usd, updated_at
                   FROM market_calibration ORDER BY n_samples DESC"""
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {}
    return {
        r[0]: {
            "calibration": round(float(r[1]), 4),
            "n_samples": int(r[2]),
            "last_ratio": round(float(r[3]), 4) if r[3] is not None else None,
            "last_predicted_usd": round(float(r[4]), 2) if r[4] is not None else None,
            "last_actual_usd": round(float(r[5]), 2) if r[5] is not None else None,
            "updated_at": r[6],
        }
        for r in rows
    }


def _self_test() -> None:
    import os, tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        ensure_schema(path)

        # First observation seeds the value
        c = update("KXBRENTD-26JUN0117-T100", predicted_usd=10.0,
                   actual_usd=2.5, db_path=path)
        assert c is not None and abs(c - 0.25) < 0.001, c

        # EWMA toward 0.50: 0.05*0.50 + 0.95*0.25 = 0.2625
        c = update("KXBRENTD", predicted_usd=10.0,
                   actual_usd=5.0, db_path=path)
        assert abs(c - 0.2625) < 0.001, c

        # Several observations should drift slowly
        for _ in range(20):
            c = update("KXBRENTD", predicted_usd=10.0,
                       actual_usd=5.0, db_path=path)
        # After ~20 obs at 0.5, should be close to 0.5 (specifically ~0.41)
        assert c is not None and 0.35 < c < 0.55, c

        # Below-threshold predicted → skip
        c2 = update("KXTINY", predicted_usd=0.10,
                    actual_usd=0.05, db_path=path)
        assert c2 is None

        # Lookup with feature flag OFF (default) → fallback
        v = calib_for("KXBRENTD-26JUN", fallback=0.25, db_path=path)
        # PER_MARKET_CALIB_ENABLED=False by default → fallback even with data
        assert v == 0.25

        # Now flip flag on, fetch
        settings.PER_MARKET_CALIB_ENABLED = True
        try:
            v = calib_for("KXBRENTD-26JUN", fallback=0.25, min_samples=5, db_path=path)
            assert 0.35 < v < 0.55, f"expected EWMA value, got {v}"
            # Unknown key → fallback
            v_unk = calib_for("KXUNKNOWN", fallback=0.10, db_path=path)
            assert v_unk == 0.10
        finally:
            settings.PER_MARKET_CALIB_ENABLED = False

        # calib_dict
        d = calib_dict(path)
        assert "KXBRENTD" in d and d["KXBRENTD"]["n_samples"] == 22

        print(f"calibration_ewma self-test: PASS (final KXBRENTD calib={c:.4f}, "
              f"n=22)")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
