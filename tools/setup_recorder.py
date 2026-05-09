"""Setup Outcome Recorder — runs nightly to feed the setup_outcomes corpus.

For every market that settled in the last 24h with non-zero outcome (rebate
or realized P&L), record its SETUP profile (pool, target, df, hours-to-settle
at record time, observed competition) and its OUTCOME (paid rebate, realized).

setup_recognition.py uses this corpus to score new candidates by similarity
to historical winners. Without nightly recording, the corpus never grows
and the system can't learn from outcomes.

Backfill mode: pulls ALL historical settlements (with outcome) on first run.
After that, only NEW settlements (idempotent on market_ticker).

Cron: every day at 02:00 UTC (after most settlements have flowed).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine.setup_recognition import record_outcome, ensure_schema

_log = logging.getLogger(__name__)


def record_recent(window_hours: int = 24, db_path: str = settings.DB_PATH) -> int:
    """Record outcomes for markets settled in the window. Returns count recorded."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        # Settlements with non-zero outcome that we haven't recorded yet
        rows = conn.execute(
            """SELECT s.ticker, s.rebate_earned_usd, s.our_realized_usd, s.close_time,
                      p.series_ticker, p.reward_per_day_usd, p.target_size,
                      p.discount_factor, p.start_date, p.end_date
               FROM settlement_log s
               LEFT JOIN lip_programs p ON s.ticker = p.market_ticker
               WHERE datetime(s.close_time) > datetime('now', ?)
                 AND (s.rebate_earned_usd != 0 OR s.our_realized_usd != 0)
                 AND s.ticker NOT IN (SELECT market_ticker FROM setup_outcomes)
            """,
            (f"-{window_hours} hours",),
        ).fetchall()

        # For each settlement, compute observed_competition from snapshots
        recorded = 0
        for r in rows:
            ticker, reb, real, close_time, series, pool_d, target, df, start, end = r
            if not pool_d:
                continue
            # Average competition from snapshots (best signal we have)
            comp_row = conn.execute(
                """SELECT AVG(MAX(total_score - our_score, 0))
                   FROM lip_snapshots
                   WHERE market_ticker = ? AND total_score > 0""",
                (ticker,),
            ).fetchone()
            comp = float(comp_row[0]) if comp_row and comp_row[0] else (target or 300) * 0.4
            # Period days
            period_days = 1.0
            try:
                if start and end:
                    sd = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    ed = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    period_days = max(0.04, (ed - sd).total_seconds() / 86400)
            except Exception:
                pass
            # hours_to_settle was the lifetime when we quoted; use period_days * 24
            # as a proxy (we don't store quote-time-snapshot)
            hours = period_days * 24
            try:
                record_outcome(
                    market_ticker=ticker,
                    series_ticker=series,
                    pool_per_day=pool_d,
                    target_size=target or 300,
                    discount_factor=df or 0.5,
                    hours_to_settle_at_record=hours,
                    observed_competition=comp,
                    rebate_paid_usd=reb or 0,
                    realized_pnl_usd=real or 0,
                    settlement_at=close_time,
                    period_days=period_days,
                    db_path=db_path,
                )
                recorded += 1
            except Exception as e:
                _log.warning(f"setup_recorder: failed to record {ticker}: {e}")
        return recorded
    finally:
        conn.close()


def backfill(db_path: str = settings.DB_PATH) -> int:
    """One-shot backfill — pull ALL historical settlements with outcome."""
    return record_recent(window_hours=24 * 365, db_path=db_path)


def report(db_path: str = settings.DB_PATH) -> None:
    """Print current setup_outcomes corpus stats + top setups."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        n, mean_paid, max_paid = conn.execute(
            """SELECT COUNT(*), ROUND(AVG(paid_per_day_usd),3),
                      ROUND(MAX(paid_per_day_usd),3)
               FROM setup_outcomes"""
        ).fetchone() or (0, 0, 0)
        print(f"\n=== SETUP OUTCOMES CORPUS: {n} records ===")
        print(f"  mean paid/day: ${mean_paid}, max: ${max_paid}")
        print()
        print(f"{'TICKER':<38} {'CADENCE':<8} {'POOL/d':>7} {'TARGET':>7} {'COMP':>6} {'PAID/d':>8}")
        print("-" * 90)
        for r in conn.execute(
            """SELECT market_ticker, cadence_class, pool_per_day_usd, target_size,
                      observed_competition, paid_per_day_usd
               FROM setup_outcomes
               ORDER BY paid_per_day_usd DESC LIMIT 15"""
        ):
            print(f"{r[0][:36]:<38} {r[1]:<8} ${r[2]:>5.0f} {r[3]:>7.0f} "
                  f"{r[4] or 0:>6.0f} ${r[5]:>+6.3f}")
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true",
                   help="One-shot: pull ALL historical settlements")
    p.add_argument("--report", action="store_true",
                   help="Show current corpus stats")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.report:
        report()
    elif a.backfill:
        n = backfill()
        print(f"backfill: recorded {n} setups")
        report()
    else:
        n = record_recent()
        print(f"recorded {n} new setups in last 24h")
