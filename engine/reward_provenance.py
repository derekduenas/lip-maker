"""Reward provenance — the difference between money we MODELLED and money
Kalshi PAID us (2026-09-21).

Why this module exists
----------------------
`tools/settlement_reconciler.py` computed an estimated LIP rebate from our own
snapshot model, wrote it into `settlement_log.rebate_earned_usd` and
`net_outcome_usd` — columns whose names assert realized fact — and then fed it
to `engine/calibration_ewma.update(actual_usd=...)` as ground truth. That is
circular: the model's output calibrated the model. It could not detect its own
error, and it terminated in `tools/go_live_check.py`, which sums
`net_outcome_usd` to decide whether real money may be traded.

The fix is a type distinction the schema enforces, not a convention:

    ESTIMATED  a number our model produced. Useful for ranking and for
               noticing that something is probably working. NEVER admissible
               as evidence of profit, and never an input to calibration.

    PAID       a number that came from Kalshi — a settlement statement, a
               payment record, an API-reported credit — and was reconciled
               against an independent source. Only this may calibrate, and
               only this may support a profitability claim.

Column contract after this change
---------------------------------
    rebate_estimated_usd    model output (was: silently in rebate_earned_usd)
    rebate_paid_usd         reconciled payment, NULL until one exists
    rebate_earned_usd       DEPRECATED alias, now mirrors rebate_paid_usd
    net_outcome_usd         our_realized_usd + rebate_paid_usd  (cash only)
    net_outcome_estimated_usd  our_realized_usd + rebate_estimated_usd
    reward_provenance       'paid' | 'estimate' | 'none'

`rebate_earned_usd` is kept so that the ~20 existing readers do not silently
read a missing column; it now carries PAID amounts only, so those readers
become conservative (they see 0 until a real payment is reconciled) rather
than optimistic. That is the correct direction to fail.

Migration is non-destructive: historical `rebate_earned_usd` values were
estimates, so they move to `rebate_estimated_usd`, `reward_provenance` becomes
'estimate', and the paid column stays NULL. Nothing is deleted.
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

# Values for settlement_log.reward_provenance.
PROV_PAID = "paid"          # reconciled against an independent Kalshi record
PROV_ESTIMATE = "estimate"  # our model's output
PROV_NONE = "none"          # no reward information at all

# Sources accepted as PAID. A caller must name which independent record the
# figure came from; "because our model said so" is not one of them.
PAID_SOURCES = frozenset({
    "kalshi_statement",   # downloaded/settlement statement
    "kalshi_api",         # an API-reported credit
    "operator_receipt",   # operator-supplied payment record, reconciled
})

_ADDED_COLUMNS = (
    ("rebate_estimated_usd", "REAL"),
    ("rebate_paid_usd", "REAL"),
    ("net_outcome_estimated_usd", "REAL"),
    ("reward_provenance", "TEXT"),
    ("reward_source", "TEXT"),
    ("reward_reconciled_at", "TEXT"),
)


def ensure_schema(db_path: Optional[str] = None) -> bool:
    """Add provenance columns to settlement_log. Idempotent.

    Returns True when the table exists (or was extended), False when there is
    no settlement_log yet (fresh install — init_db creates it)."""
    db_path = db_path or settings.DB_PATH
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(settlement_log)").fetchall()}
        if not cols:
            return False
        for name, ddl in _ADDED_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE settlement_log ADD COLUMN {name} {ddl}")
        conn.commit()
        return True
    finally:
        conn.close()


def migrate_estimates(db_path: Optional[str] = None) -> dict:
    """Quarantine historical estimates that were stored as if they were paid.

    Every pre-existing `rebate_earned_usd` was produced by
    settlement_reconciler._estimate_rebate. Move it to the estimated column,
    label it, and zero the paid-only columns. Non-destructive and idempotent:
    rows already carrying a provenance label are left alone.
    """
    db_path = db_path or settings.DB_PATH
    if not ensure_schema(db_path):
        return {"migrated": 0, "skipped": "no settlement_log"}
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cur = conn.execute(
            """UPDATE settlement_log
                  SET rebate_estimated_usd = COALESCE(rebate_estimated_usd, rebate_earned_usd),
                      net_outcome_estimated_usd = COALESCE(
                          net_outcome_estimated_usd,
                          COALESCE(our_realized_usd, 0) + COALESCE(rebate_earned_usd, 0)),
                      rebate_paid_usd = NULL,
                      rebate_earned_usd = 0,
                      net_outcome_usd = COALESCE(our_realized_usd, 0),
                      reward_provenance = ?,
                      reward_source = 'legacy_model_estimate'
                WHERE reward_provenance IS NULL""",
            (PROV_ESTIMATE,),
        )
        n = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    if n:
        _log.warning(
            f"reward_provenance: quarantined {n} settlement_log rows whose "
            f"rebate_earned_usd was a model estimate stored as actual. "
            f"net_outcome_usd now reflects realized trading cash only; the "
            f"estimate is preserved in rebate_estimated_usd."
        )
    return {"migrated": n}


def record_estimate(conn: sqlite3.Connection, ticker: str, amount_usd: float,
                    *, realized_usd: float = 0.0) -> None:
    """Store a MODEL estimate. Never touches the paid columns."""
    conn.execute(
        """UPDATE settlement_log
              SET rebate_estimated_usd = ?,
                  net_outcome_estimated_usd = ?,
                  reward_provenance = CASE WHEN reward_provenance = ?
                                           THEN ? ELSE ? END,
                  reward_source = COALESCE(reward_source, 'model_estimate')
            WHERE ticker = ?""",
        (float(amount_usd), float(realized_usd) + float(amount_usd),
         PROV_PAID, PROV_PAID, PROV_ESTIMATE, ticker),
    )


def record_payment(ticker: str, amount_usd: float, *, source: str,
                   db_path: Optional[str] = None) -> dict:
    """Record a RECONCILED payment — the only thing that may calibrate.

    `source` must name an independent record (see PAID_SOURCES). This is the
    single entry point that may write rebate_paid_usd / rebate_earned_usd.
    """
    if source not in PAID_SOURCES:
        raise ValueError(
            f"source {source!r} is not an independent payment record; "
            f"expected one of {sorted(PAID_SOURCES)}. Model output is not a payment.")
    amount = float(amount_usd)
    if amount < 0 or amount != amount:
        raise ValueError(f"invalid payment amount: {amount_usd!r}")
    db_path = db_path or settings.DB_PATH
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cur = conn.execute(
            """UPDATE settlement_log
                  SET rebate_paid_usd = ?,
                      rebate_earned_usd = ?,
                      net_outcome_usd = COALESCE(our_realized_usd, 0) + ?,
                      reward_provenance = ?,
                      reward_source = ?,
                      reward_reconciled_at = ?
                WHERE ticker = ?""",
            (amount, amount, amount, PROV_PAID, source,
             datetime.now(timezone.utc).isoformat(), ticker),
        )
        conn.commit()
        return {"ticker": ticker, "updated": cur.rowcount or 0, "amount_usd": amount}
    finally:
        conn.close()


def paid_total(db_path: Optional[str] = None) -> float:
    """Sum of independently reconciled reward payments. 0.0 when none."""
    db_path = db_path or settings.DB_PATH
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(rebate_paid_usd), 0) FROM settlement_log "
                "WHERE reward_provenance = ?", (PROV_PAID,)).fetchone()
            return float(row[0] or 0.0)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0.0


def has_paid_evidence(db_path: Optional[str] = None) -> bool:
    """True when at least one reconciled payment exists.

    Gates that authorize real money should require this: without it, every
    reward number in the system is a model output."""
    return paid_total(db_path) > 0.0
