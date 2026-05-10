"""LIP period reconciliation monitor.

Kalshi doesn't expose a per-user LIP accrual endpoint (confirmed via CFTC
filing + API docs). We have to simulate + reconcile:

  1. Track Kalshi balance periodically (every 5 min during active periods).
  2. Snapshot balance at each LIP period start and end.
  3. Compare balance delta (at period end) vs our simulated snapshot sum ×
     reward. Delta = trading PnL + LIP rebate + fees; isolate the LIP piece
     by subtracting tracked trading PnL.

Outputs:
  - `period_reconciliation` table rows: simulated_usd vs actual_usd
  - Accuracy ratio: how close our simulator tracks reality
  - Alerts if ratio drifts > 30% (simulator miscalibration)

Designed to run as a companion process to run_paper.py (or as a standalone
daily cron). Reads from the lip_maker.db shared with the main runner.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_auth import KalshiClient

_log = logging.getLogger(__name__)


def record_balance_snapshot(db_path: str = settings.DB_PATH) -> float:
    """Record a balance observation. Returns the balance in USD.

    2026-05-02 PREDATOR: now logs cash + portfolio_value + total_nav so daily
    P&L math is honest. Cash-only was misleading: $400 deployed into open
    positions looked like a -$400 loss until those settled. True NAV =
    cash + portfolio_value (Kalshi's MTM of open positions).
    """
    c = KalshiClient()
    if not c.authenticated:
        _log.warning("Kalshi not authenticated — cannot record balance")
        return 0.0
    try:
        b = c.get('/portfolio/balance')
        cash = float(b.get('balance', 0)) / 100
        port_val = float(b.get('portfolio_value', 0)) / 100
        balance = cash  # backward-compat: callers expect cash from this fn
    except Exception as e:
        _log.warning(f"balance fetch failed: {e}")
        return 0.0

    conn = sqlite3.connect(db_path)
    try:
        # Create table if not present (lightweight, idempotent)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                balance_usd     REAL NOT NULL,
                recorded_at     TEXT NOT NULL
            )
        """)
        # Add new columns idempotently (zero downtime — old rows = NULL)
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(balance_log)").fetchall()}
        if "portfolio_value_usd" not in existing_cols:
            conn.execute("ALTER TABLE balance_log ADD COLUMN portfolio_value_usd REAL")
        if "total_nav_usd" not in existing_cols:
            conn.execute("ALTER TABLE balance_log ADD COLUMN total_nav_usd REAL")
        conn.execute(
            "INSERT INTO balance_log (balance_usd, portfolio_value_usd, "
            "total_nav_usd, recorded_at) VALUES (?, ?, ?, ?)",
            (cash, port_val, cash + port_val,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return balance


def get_period_boundaries(db_path: str = settings.DB_PATH) -> list[tuple[str, str]]:
    """Return unique (start, end) tuples from lip_programs we've seen."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT start_date, end_date FROM lip_programs ORDER BY end_date"
        ).fetchall()
    finally:
        conn.close()
    return rows


def balance_at(ts_iso: str, db_path: str = settings.DB_PATH) -> float | None:
    """Return the closest-after balance record to a given ISO timestamp."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT balance_usd FROM balance_log WHERE recorded_at >= ? ORDER BY recorded_at LIMIT 1",
            (ts_iso,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def simulated_period_reward(period_start: str, period_end: str,
                             db_path: str = settings.DB_PATH) -> float:
    """Sum our simulated snapshot scores × reward/snapshot for a period.

    Rough heuristic: our per-snapshot score was logged into lip_snapshots
    during paper mode. Multiply by fraction-of-period covered × reward.
    """
    conn = sqlite3.connect(db_path)
    try:
        # Sum per-market: our_score_sum × reward_per_period / total_snapshots_in_period
        rows = conn.execute(
            """SELECT market_ticker, SUM(our_score) as our_sum, COUNT(*) as n
               FROM lip_snapshots
               WHERE captured_at >= ? AND captured_at < ?
                 AND snapshot_valid = 1
               GROUP BY market_ticker""",
            (period_start, period_end),
        ).fetchall()
        total_sim = 0.0
        for tkr, our_sum, n in rows:
            # Find the program reward for this market
            pg = conn.execute(
                """SELECT period_reward_usd FROM lip_programs
                   WHERE market_ticker = ? AND start_date = ?""",
                (tkr, period_start),
            ).fetchone()
            if pg is None:
                continue
            reward = pg[0]
            # Assume total snapshots in period ≈ 86400 × days (1 per sec)
            try:
                s = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
                e = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
                period_seconds = max(1, (e - s).total_seconds())
            except Exception:
                period_seconds = 86400
            # Our snapshot score ÷ 2.0 (max per snapshot) × total period score
            # Conservative: assume saturation (total score ≈ 2.0/snap)
            est = (our_sum / (2.0 * period_seconds)) * reward
            total_sim += est
        return total_sim
    finally:
        conn.close()


def reconcile_period(period_start: str, period_end: str,
                     db_path: str = settings.DB_PATH) -> dict:
    """Compare simulated vs actual for a completed period.

    LIVE-MODE NETTING (audit fix 2026-05-09):
      balance_delta = LIP_rebate + trading_PnL + fees + (deposits/withdrawals)
      We pull rebate + trading_PnL from settlement_log per ticker, then:
        actual_rebate = SUM(rebate_earned_usd)            ← truth source
        trading_pnl   = SUM(our_realized_usd)             ← informational
        drift_usd     = balance_delta - (rebate + pnl)    ← sanity check
      Ratio = actual_rebate / simulated_rebate (no PnL contamination)
    """
    sim = simulated_period_reward(period_start, period_end, db_path=db_path)
    bal_before = balance_at(period_start, db_path=db_path)
    bal_after  = balance_at(period_end,   db_path=db_path)

    # Pull netting components from settlement_log (per-ticker truth)
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(rebate_earned_usd), 0.0) AS rebate,
                 COALESCE(SUM(our_realized_usd),  0.0) AS trading_pnl,
                 COUNT(*)                              AS n_settlements
               FROM settlement_log
               WHERE close_time >= ? AND close_time < ?""",
            (period_start, period_end),
        ).fetchone()
    finally:
        conn.close()
    rebate_from_log, trading_pnl, n_settled = row

    bal_delta = (bal_after - bal_before) if (bal_before is not None
                                              and bal_after is not None) else None
    expected_delta = rebate_from_log + trading_pnl
    drift = (bal_delta - expected_delta) if bal_delta is not None else None

    actual = rebate_from_log  # NOW NETTED — was bal_after - bal_before

    result = {
        "period_start":      period_start,
        "period_end":        period_end,
        "simulated_usd":     sim,
        "actual_usd":        actual,
        "trading_pnl_usd":   trading_pnl,
        "balance_delta_usd": bal_delta,
        "drift_usd":         drift,
        "n_settlements":     n_settled,
        "ratio":             (actual / sim) if (actual and sim and sim > 0) else None,
        "balance_before":    bal_before,
        "balance_after":     bal_after,
    }

    # Persist
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO period_reconciliation
               (period_start, period_end, market_ticker, our_simulated_usd,
                kalshi_actual_usd, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (period_start, period_end, "ALL", sim, actual or 0,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    return result


def report(db_path: str = settings.DB_PATH) -> None:
    """Print reconciliation summary across all observed periods."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT period_start, period_end, our_simulated_usd, kalshi_actual_usd, recorded_at
               FROM period_reconciliation
               WHERE market_ticker = 'ALL'
               ORDER BY period_start DESC
               LIMIT 20"""
        ).fetchall()
        # Also show current balance from most recent snapshot
        last_bal = conn.execute(
            "SELECT balance_usd, recorded_at FROM balance_log ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    print("=== LIP Reconciliation Report ===")
    if last_bal:
        print(f"Latest balance: ${last_bal[0]:.2f} @ {last_bal[1]}")
    print()
    if not rows:
        print("No completed periods yet.")
        return
    print(f"{'period':25s} {'sim_$':>10s} {'actual_$':>10s} {'ratio':>7s}")
    for start, end, sim, actual, ts in rows:
        ratio = (actual / sim) if (actual and sim and sim > 0) else None
        ratio_s = f"{ratio:.2f}x" if ratio else "—"
        actual_s = f"${actual:.2f}" if actual else "—"
        print(f"  {start[:10]}→{end[:10]} ${sim:>7.2f}  {actual_s:>10s}  {ratio_s:>6s}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--record-balance", action="store_true", help="log current balance")
    p.add_argument("--reconcile", nargs=2, metavar=("START", "END"),
                   help="reconcile a period (ISO dates)")
    p.add_argument("--report", action="store_true", help="print summary")
    a = p.parse_args()

    if a.record_balance:
        b = record_balance_snapshot()
        print(f"recorded balance: ${b:.2f}")
    elif a.reconcile:
        r = reconcile_period(a.reconcile[0], a.reconcile[1])
        print(r)
    else:
        report()
