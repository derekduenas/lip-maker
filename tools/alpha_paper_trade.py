"""Alpha paper-trade harness — validate predictions against actual outcomes.

Pretends every alpha_predictions row was acted on at the recommended size.
Tracks hypothetical P&L. After 7 days of paper data, we know if our edge
predictions actually produce profit BEFORE risking real money.

PROCESS:
  1. Read pending alpha_predictions
  2. For each: compute "paper trade" — simulate filling at market price
  3. Track in alpha_paper_trades table
  4. Once each market settles, compute realized P&L
  5. Roll up: hit rate, total P&L, by-pillar performance

This is the GATE between Brain proposing trades and Brain executing real trades.
After 7 days of paper validation showing positive ROI → unlock TIER_2 auto-trade.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS alpha_paper_trades (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  prediction_id   INTEGER NOT NULL UNIQUE,
  ts_entered      TEXT NOT NULL,
  pillar          TEXT NOT NULL,
  market_ticker   TEXT NOT NULL,
  side            TEXT NOT NULL,
  count           INTEGER NOT NULL,
  entry_price_c   INTEGER NOT NULL,
  cost_usd        REAL NOT NULL,
  fair_prob       REAL NOT NULL,
  status          TEXT NOT NULL DEFAULT 'open',
  settled_at      TEXT,
  exit_price_c    INTEGER,
  realized_usd    REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON alpha_paper_trades(status);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def open_paper_trades(db_path: str = settings.DB_PATH) -> int:
    """Open paper trades for every pending alpha_predictions row."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    opened = 0
    try:
        rows = conn.execute("""
            SELECT p.id, p.pillar, p.market_ticker, p.market_yes_price,
                   p.fair_prob, p.direction, p.recommended_usd, p.confidence
            FROM alpha_predictions p
            LEFT JOIN alpha_paper_trades t ON t.prediction_id = p.id
            WHERE p.status = 'pending' AND t.id IS NULL
              AND datetime(p.ts) > datetime('now', '-2 hours')
        """).fetchall()
        for r in rows:
            pid, pillar, ticker, market_yes, fair_prob, direction, usd, conf = r
            entry_price_c = int(market_yes * 100) if direction == "yes" else int((1 - market_yes) * 100)
            if entry_price_c <= 0 or entry_price_c >= 100:
                continue
            count = max(1, int(usd / (entry_price_c / 100.0)))
            cost = (entry_price_c / 100.0) * count
            conn.execute(
                """INSERT OR IGNORE INTO alpha_paper_trades
                   (prediction_id, ts_entered, pillar, market_ticker,
                    side, count, entry_price_c, cost_usd, fair_prob)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, datetime.now(timezone.utc).isoformat(),
                 pillar, ticker, direction, count,
                 entry_price_c, cost, fair_prob),
            )
            conn.execute("UPDATE alpha_predictions SET status = 'paper_open' WHERE id = ?", (pid,))
            opened += 1
        conn.commit()
    finally:
        conn.close()
    return opened


def settle_paper_trades(db_path: str = settings.DB_PATH) -> int:
    """For paper trades whose market has settled in Kalshi, compute realized."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    settled = 0
    try:
        rows = conn.execute("""
            SELECT t.id, t.market_ticker, t.side, t.count, t.entry_price_c, t.cost_usd
            FROM alpha_paper_trades t
            WHERE t.status = 'open'
        """).fetchall()
        for r in rows:
            tid, ticker, side, count, entry_c, cost = r
            settle_row = conn.execute(
                "SELECT close_time, our_realized_usd FROM settlement_log WHERE ticker = ?",
                (ticker,),
            ).fetchone()
            if not settle_row:
                continue
            close_time = settle_row[0]
            # Determine outcome by querying Kalshi market settlement
            # (use existing settlement_log infrastructure)
            try:
                from execution.kalshi_auth import KalshiClient
                c = KalshiClient()
                m_resp = c.get(f"/markets/{ticker}")
                m_info = m_resp.get("market", {}) or {}
                result = m_info.get("result", "").lower()  # "yes"/"no"/""
            except Exception:
                continue
            if result not in ("yes", "no"):
                continue
            # Paper P&L: if our side won → payoff = $1/contract; if lost → 0
            won = (result == side)
            if won:
                exit_c = 100
                realized = (1.00 - entry_c / 100.0) * count
            else:
                exit_c = 0
                realized = -(entry_c / 100.0) * count
            conn.execute(
                """UPDATE alpha_paper_trades
                   SET status = 'settled', settled_at = ?,
                       exit_price_c = ?, realized_usd = ?
                   WHERE id = ?""",
                (close_time, exit_c, realized, tid),
            )
            settled += 1
        conn.commit()
    finally:
        conn.close()
    return settled


def report(db_path: str = settings.DB_PATH) -> None:
    """Show current paper-trade book + realized P&L."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        n_open = conn.execute(
            "SELECT COUNT(*) FROM alpha_paper_trades WHERE status='open'"
        ).fetchone()[0]
        row = conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN realized_usd > 0 THEN 1 ELSE 0 END),
                      ROUND(SUM(realized_usd), 2),
                      ROUND(SUM(cost_usd), 2)
               FROM alpha_paper_trades WHERE status='settled'"""
        ).fetchone()
        n_settled, n_won, total_pnl, total_cost = row or (0, 0, 0, 0)
        n_settled = n_settled or 0
        n_won = n_won or 0
        total_pnl = total_pnl or 0
        total_cost = total_cost or 0
        win_rate = (n_won / n_settled * 100) if n_settled else 0
        roi = (total_pnl / max(0.01, total_cost) * 100) if total_cost else 0
        print(f"\n📊 ALPHA PAPER-TRADE BOOK")
        print(f"  open: {n_open}  settled: {n_settled}  won: {n_won} ({win_rate:.1f}%)")
        print(f"  total cost: ${total_cost:.2f}  realized: ${total_pnl:+.2f}  ROI: {roi:+.1f}%")
        print()
        print("Recent settled (last 10):")
        for r in conn.execute(
            """SELECT pillar, market_ticker, side, count, entry_price_c,
                      exit_price_c, ROUND(realized_usd,2) pnl, settled_at
               FROM alpha_paper_trades WHERE status='settled'
               ORDER BY settled_at DESC LIMIT 10"""
        ):
            print(f"  [{r[0]:<7}] {r[1][:28]:<30} {r[2]:>3} ct={r[3]:>3} "
                  f"@{r[4]}c→{r[5]}c  ${r[6]:>+6.2f}")
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--open", action="store_true", help="Open paper trades from pending predictions")
    p.add_argument("--settle", action="store_true", help="Settle paper trades whose markets resolved")
    p.add_argument("--report", action="store_true", help="Show paper-book status")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    if a.open:
        n = open_paper_trades()
        print(f"opened {n} paper trades")
    if a.settle:
        n = settle_paper_trades()
        print(f"settled {n} paper trades")
    if a.report or not (a.open or a.settle):
        report()
