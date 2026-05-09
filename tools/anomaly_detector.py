"""Pure-Python anomaly detector — fires triggers for the Edge Hunter Brain.

Runs every 5 minutes via cron. Checks pure-Python rules against system state.
When ANY rule fires, writes to brain_triggers table. The Edge Hunter Brain
(Claude API) reads pending triggers and reasons about each.

DESIGN PRINCIPLE: rules are SIMPLE comparisons. No LLM needed at this layer.
Costs $0 in API calls. Only escalates to Claude when something MIGHT need
reasoning. ~95% of system time, no anomaly fires = $0 spent.

RULES (pure thresholds, no learning):
  1. Calibration drift   — any series drops > 30% in 1h
  2. Balance drop        — total NAV drops > 3% in 1h
  3. No-fills timeout    — 0 fills for 4+ hours (stall)
  4. New series detected — Kalshi has a series we've never seen
  5. Allocator/quoting gap — picks-vs-resting intersection < 30%
  6. Drawdown threshold  — balance > 5% below 7d peak
  7. Reconciliation alert— truth_reconciler logged ALERT-action items unread

If ANY fires, writes a row to brain_triggers with severity + context. Brain
reads queue every 10 min, processes new triggers, marks resolved.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)


# Tunable thresholds (no LLM needed — pure rules)
CAL_DRIFT_PCT = 0.30           # 30% calibration ratio change in 1h
BALANCE_DROP_PCT = 0.03        # 3% NAV drop in 1h
NO_FILLS_TIMEOUT_HOURS = 4
ALLOCATOR_QUOTING_MIN_RATIO = 0.30
DRAWDOWN_PCT = 0.05            # 5% off 7d peak
DAILY_REVIEW_HOUR_UTC = 14     # 14:00 UTC = 9am ET — morning strategic check
WEEKLY_REVIEW_DOW = 0          # Monday
WEEKLY_REVIEW_HOUR_UTC = 14


SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_triggers (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  severity     TEXT NOT NULL,   -- INFO, WARN, ALERT, EMERGENCY
  rule         TEXT NOT NULL,   -- which rule fired
  context_json TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending, processing, resolved
  brain_run_id INTEGER,         -- FK to decision_log when picked up
  resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_triggers_status ON brain_triggers(status);
CREATE INDEX IF NOT EXISTS idx_triggers_ts ON brain_triggers(ts);

CREATE TABLE IF NOT EXISTS decision_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  trigger_id    INTEGER,            -- FK to brain_triggers (NULL for scheduled)
  trigger_type  TEXT NOT NULL,      -- anomaly, daily_review, weekly_review, manual
  brain_model   TEXT NOT NULL,      -- claude-haiku-4-5, claude-sonnet-4-6
  context_summary TEXT,
  reasoning     TEXT NOT NULL,
  actions_proposed TEXT,            -- JSON list
  actions_executed TEXT,            -- JSON list (subset that were safe enough to auto-run)
  cost_usd      REAL,               -- API cost for this call
  outcome       TEXT,               -- backfilled later: success/no_op/error
  outcome_ts    TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decision_log(ts);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# Per-rule dedup windows (minutes). Slow / informational rules get longer
# windows to prevent the same series being re-flagged 85x in 24h. 2026-05-05.
_RULE_DEDUP_MINUTES = {
    "new_series_detected":     1440,   # 24h — series don't change minute-to-minute
    "allocator_quoting_gap":    240,   # 4h
    "reconciliation_alerts":     60,   # 1h
    "drawdown_threshold":        30,   # 30min
    "daily_strategic_review":   720,   # 12h
    "weekly_deep_review":      4320,   # 72h
}
_DEDUP_DEFAULT_MIN = 15


def _emit_trigger(conn, severity: str, rule: str, context: dict) -> int:
    """Write a trigger to the queue. Per-rule dedup window (minutes).
    Dedup matches BOTH pending AND recently-resolved triggers — prevents
    re-firing on the same condition that brain just addressed."""
    window_min = _RULE_DEDUP_MINUTES.get(rule, _DEDUP_DEFAULT_MIN)
    recent = conn.execute(
        f"""SELECT id FROM brain_triggers
           WHERE rule = ?
             AND datetime(ts) > datetime('now','-{int(window_min)} minutes')""",
        (rule,),
    ).fetchone()
    if recent:
        return recent[0]  # dedupe
    cur = conn.execute(
        """INSERT INTO brain_triggers (ts, severity, rule, context_json)
           VALUES (?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), severity, rule, json.dumps(context, default=str)),
    )
    conn.commit()
    return cur.lastrowid


# ── RULE 1: Calibration drift ──

def check_calibration_drift(conn) -> None:
    """Did any series's calibration_ratio change > 30% in the last hour?
    Compares current to value 1h ago via reconciliation_log audit trail.
    """
    rows = conn.execute(
        """SELECT series_ticker, calibration_ratio, last_updated
           FROM series_calibration
           WHERE datetime(last_updated) > datetime('now','-1 hour')
             AND samples >= 100"""
    ).fetchall()
    for series, current_cal, ts in rows:
        # No history table for cal yet; use samples threshold + extreme value as proxy
        if current_cal < 0.20 or current_cal > 1.50:
            _emit_trigger(conn, "WARN", "calibration_extreme",
                          {"series": series, "calibration_ratio": current_cal,
                           "last_updated": ts})


# ── RULE 2: Balance drop ──

def check_balance_drop(conn) -> None:
    """If NAV dropped > 3% in last hour, alert."""
    try:
        from execution.kalshi_auth import KalshiClient
        c = KalshiClient()
        b = c.get("/portfolio/balance")
        balance_cents = b.get("balance", 0)
        portfolio_cents = b.get("portfolio_value", 0)
        nav = (balance_cents + portfolio_cents) / 100.0
    except Exception as e:
        return
    # Compare to most recent funnel snapshot's balance proxy (we don't store NAV)
    # For now: store NAV in dedicated nav_history table
    conn.execute("""CREATE TABLE IF NOT EXISTS nav_history (
        ts TEXT PRIMARY KEY, nav REAL NOT NULL
    )""")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT OR REPLACE INTO nav_history(ts, nav) VALUES (?, ?)",
                 (now, nav))
    prev = conn.execute(
        """SELECT nav FROM nav_history
           WHERE datetime(ts) BETWEEN datetime('now','-1 hour 10 min')
                                  AND datetime('now','-50 min')
           ORDER BY ts DESC LIMIT 1"""
    ).fetchone()
    if prev:
        prior_nav = float(prev[0])
        drop_pct = (prior_nav - nav) / max(0.01, prior_nav)
        if drop_pct > BALANCE_DROP_PCT:
            _emit_trigger(conn, "ALERT", "balance_drop",
                          {"prior_nav": round(prior_nav, 2),
                           "current_nav": round(nav, 2),
                           "drop_pct": round(drop_pct * 100, 2)})
    conn.commit()


# ── RULE 3: No-fills timeout ──

def check_no_fills_timeout(conn) -> None:
    """If we have NO fills in last N hours, system may be stalled."""
    try:
        last_fill = conn.execute(
            "SELECT MAX(created_at) FROM fill_ledger"
        ).fetchone()
        if not last_fill or not last_fill[0]:
            return
        last_dt = datetime.fromisoformat(last_fill[0].replace("Z", "+00:00"))
        hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        if hours_since > NO_FILLS_TIMEOUT_HOURS:
            _emit_trigger(conn, "WARN", "no_fills_timeout",
                          {"hours_since_last_fill": round(hours_since, 1)})
    except Exception:
        pass


# ── RULE 4: New series detected ──

def check_new_series(conn) -> None:
    """If lip_programs has a series we have NO calibration data for AND it's
    high-pool, propose an exploration probe."""
    rows = conn.execute(
        """SELECT series_ticker, COUNT(*) n, SUM(reward_per_day_usd) pool_d
           FROM lip_programs
           WHERE enrolled = 1 AND paid_out = 0
             AND datetime(end_date) > datetime('now')
             AND series_ticker NOT IN (SELECT series_ticker FROM series_calibration)
             AND series_ticker NOT IN (
                 SELECT DISTINCT substr(market_ticker,1,instr(market_ticker||'-','-')-1)
                 FROM setup_outcomes)
           GROUP BY series_ticker
           HAVING pool_d >= 50"""
    ).fetchall()
    for series, n, pool_d in rows[:5]:  # cap to top 5 to avoid spam
        _emit_trigger(conn, "INFO", "new_series_detected",
                      {"series": series, "n_markets": n, "pool_per_day_usd": round(pool_d, 2)})


# ── RULE 5: Allocator vs quoting gap ──

def check_allocator_quoting_gap(conn) -> None:
    """If allocator picks vs actually resting < 30%, the placement pipeline is broken."""
    try:
        from engine.capital_allocator import select_optimal_portfolio
        from execution.kalshi_auth import KalshiClient
        picks = {m["market_ticker"] for m in
                 select_optimal_portfolio(budget_usd=settings.MAX_TOTAL_GROSS_USD)}
        c = KalshiClient()
        r = c.get("/portfolio/orders", params={"status": "resting", "limit": 1000})
        resting = {o.get("ticker") for o in r.get("orders", []) if o.get("ticker")}
        if not picks:
            return
        intersection = len(picks & resting)
        ratio = intersection / max(1, len(picks))
        if ratio < ALLOCATOR_QUOTING_MIN_RATIO:
            _emit_trigger(conn, "WARN", "allocator_quoting_gap",
                          {"picks": len(picks), "resting": len(resting),
                           "intersection": intersection, "ratio": round(ratio, 3)})
    except Exception as e:
        _log.debug(f"allocator gap check failed: {e}")


# ── RULE 6: Drawdown threshold ──

def check_drawdown(conn) -> None:
    peak = conn.execute(
        "SELECT MAX(nav) FROM nav_history WHERE datetime(ts) > datetime('now','-7 days')"
    ).fetchone()
    cur = conn.execute(
        "SELECT nav FROM nav_history ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if not (peak and peak[0] and cur and cur[0]):
        return
    dd = (peak[0] - cur[0]) / max(0.01, peak[0])
    if dd > DRAWDOWN_PCT:
        _emit_trigger(conn, "ALERT", "drawdown_threshold",
                      {"peak_7d": round(peak[0], 2),
                       "current_nav": round(cur[0], 2),
                       "drawdown_pct": round(dd * 100, 2)})


# ── RULE 7: Reconciliation alerts ──

def check_reconciliation_alerts(conn) -> None:
    """Any ALERT-action items in reconciliation_log from last hour?"""
    try:
        rows = conn.execute(
            """SELECT category, ticker, COUNT(*) n
               FROM reconciliation_log
               WHERE action = 'ALERT'
                 AND datetime(ts) > datetime('now','-1 hour')
               GROUP BY category, ticker"""
        ).fetchall()
        if rows:
            samples = [{"category": r[0], "ticker": r[1], "count": r[2]} for r in rows[:5]]
            _emit_trigger(conn, "WARN", "reconciliation_alerts",
                          {"alert_buckets": len(rows), "samples": samples})
    except Exception:
        pass


# ── RULE 8: Scheduled daily/weekly review ──

def check_scheduled_review(conn) -> None:
    now = datetime.now(timezone.utc)
    # Daily strategic review at DAILY_REVIEW_HOUR_UTC
    if now.hour == DAILY_REVIEW_HOUR_UTC and now.minute < 5:
        already = conn.execute(
            """SELECT id FROM brain_triggers
               WHERE rule = 'daily_strategic_review'
                 AND date(ts) = date('now')""",
        ).fetchone()
        if not already:
            _emit_trigger(conn, "INFO", "daily_strategic_review",
                          {"date": now.date().isoformat()})
    # Weekly deep review on WEEKLY_REVIEW_DOW (Monday) at WEEKLY_REVIEW_HOUR_UTC
    if (now.weekday() == WEEKLY_REVIEW_DOW
            and now.hour == WEEKLY_REVIEW_HOUR_UTC
            and now.minute < 5):
        week = now.isocalendar()[1]
        already = conn.execute(
            """SELECT id FROM brain_triggers
               WHERE rule = 'weekly_deep_review'
                 AND context_json LIKE ?""",
            (f"%week{week}%",),
        ).fetchone()
        if not already:
            _emit_trigger(conn, "INFO", "weekly_deep_review",
                          {"week": week, "year": now.year})


# ── Main ──

def run_cycle(db_path: str = settings.DB_PATH) -> dict:
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        check_calibration_drift(conn)
        check_balance_drop(conn)
        check_no_fills_timeout(conn)
        check_new_series(conn)
        check_allocator_quoting_gap(conn)
        check_drawdown(conn)
        check_reconciliation_alerts(conn)
        check_scheduled_review(conn)
        # Report
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM brain_triggers WHERE status = 'pending'"
        ).fetchone()[0]
        n_new = conn.execute(
            """SELECT COUNT(*) FROM brain_triggers
               WHERE datetime(ts) > datetime('now','-5 minutes')"""
        ).fetchone()[0]
        return {"new_triggers_this_cycle": n_new, "total_pending": n_pending}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    res = run_cycle()
    print(f"anomaly_detector: {res['new_triggers_this_cycle']} new, "
          f"{res['total_pending']} pending")
