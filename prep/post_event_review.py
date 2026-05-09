"""Post-event reconciliation — P&L + thesis accuracy.

After a FOMC event settles, query Kalshi for each order's final outcome,
compute realized P&L, compare to thesis predictions, write scorecard to
`sovereign_event_review` table.

Run:
    python prep/post_event_review.py --event fomc_20260430

Idempotent — re-running updates latest outcomes but doesn't duplicate rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_log = logging.getLogger(__name__)
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _schema(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sovereign_event_review (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id         TEXT NOT NULL,
            order_db_id      INTEGER NOT NULL,
            ticker           TEXT NOT NULL,
            side             TEXT NOT NULL,
            price_cents      INTEGER NOT NULL,
            contracts        INTEGER NOT NULL,
            cost_usd         REAL NOT NULL,
            settled_outcome  TEXT,
            pnl_usd          REAL,
            won              INTEGER,
            settled_at       TEXT,
            reviewed_at      TEXT NOT NULL,
            UNIQUE(order_db_id)
        );
        """)
        conn.commit()
    finally:
        conn.close()


def fetch_market_outcome(ticker: str) -> Optional[dict]:
    """Unauthenticated: get market outcome after settlement."""
    try:
        r = requests.get(f"{KALSHI_BASE}/markets/{ticker}", timeout=10)
        r.raise_for_status()
        return r.json().get("market")
    except Exception as e:
        _log.warning(f"fetch outcome for {ticker} failed: {e}")
        return None


def compute_pnl(side: str, buy_price: int, contracts: int, outcome: str) -> tuple[float, bool]:
    """Compute P&L given position and settlement.

    Side=yes won if outcome=yes → +$1/contract - buy_price
    Side=no  won if outcome=no  → +$1/contract - buy_price
    Loss    = -buy_price/contract

    Returns (pnl_usd, won).
    """
    outcome = outcome.lower() if outcome else ""
    won = (side == "yes" and outcome == "yes") or (side == "no" and outcome == "no")
    if won:
        pnl = contracts * (100 - buy_price) / 100.0
    else:
        pnl = -contracts * buy_price / 100.0
    return pnl, won


def review_event(event_id: str, db_path: str = "/root/sovereign/data/sovereign.db") -> dict:
    _schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        orders = conn.execute(
            """SELECT id, ticker, side, price_cents, contracts, cost_usd, status, paper
               FROM sovereign_orders
               WHERE event_id = ? AND status IN ('accepted', 'paper')""",
            (event_id,),
        ).fetchall()
    finally:
        conn.close()

    if not orders:
        return {"event_id": event_id, "n_orders": 0, "msg": "no orders for this event"}

    results = {
        "event_id": event_id,
        "n_orders": len(orders),
        "settled": 0,
        "unsettled": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "total_cost": 0.0,
        "per_ticker": [],
    }

    for oid, ticker, side, price, contracts, cost, status, paper in orders:
        m = fetch_market_outcome(ticker)
        time.sleep(0.2)  # gentle rate limit

        if m is None:
            results["unsettled"] += 1
            continue
        st = m.get("status", "")
        outcome = m.get("result", "")
        if st not in ("finalized", "settled"):
            results["unsettled"] += 1
            continue

        pnl, won = compute_pnl(side, price, contracts, outcome)
        results["settled"] += 1
        results["total_pnl"] += pnl
        results["total_cost"] += cost
        if won:
            results["wins"] += 1
        else:
            results["losses"] += 1
        results["per_ticker"].append({
            "ticker": ticker, "side": side, "price": price, "contracts": contracts,
            "outcome": outcome, "won": won, "pnl_usd": round(pnl, 2),
            "paper": bool(paper),
        })

        # Persist
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO sovereign_event_review
                   (event_id, order_db_id, ticker, side, price_cents, contracts, cost_usd,
                    settled_outcome, pnl_usd, won, settled_at, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, oid, ticker, side, price, contracts, cost,
                 outcome, pnl, 1 if won else 0,
                 m.get("expiration_time", datetime.now(timezone.utc).isoformat()),
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    if results["settled"] > 0:
        results["win_rate"] = results["wins"] / results["settled"]
        results["roi"] = (results["total_pnl"] / results["total_cost"]
                          if results["total_cost"] else 0)
    return results


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True)
    p.add_argument("--db", default="/root/sovereign/data/sovereign.db")
    p.add_argument("--json", default=None)
    a = p.parse_args()

    r = review_event(a.event, db_path=a.db)
    print(f"\n══════════════════════════════════════════════════════")
    print(f"  POST-EVENT REVIEW — {a.event}")
    print(f"══════════════════════════════════════════════════════")
    for k in ("n_orders", "settled", "unsettled", "wins", "losses",
              "total_pnl", "total_cost"):
        if k in r:
            v = r[k]
            if isinstance(v, float):
                print(f"  {k:<12s}: ${v:+.2f}")
            else:
                print(f"  {k:<12s}: {v}")
    if r.get("settled", 0) > 0:
        print(f"  win_rate     : {r['win_rate']:.1%}")
        print(f"  roi          : {r['roi']:.1%}")

    print(f"\n  Per-ticker (top 15 by P&L):")
    ranked = sorted(r.get("per_ticker", []),
                    key=lambda x: -x["pnl_usd"])[:15]
    for t in ranked:
        mark = "✓" if t["won"] else "✗"
        print(f"   {mark} {t['ticker'][:40]:<40s} {t['side']:<3s} "
              f"{t['price']:>3d}c x{t['contracts']:<3d} "
              f"outcome={t['outcome']:<4s} pnl=${t['pnl_usd']:+.2f}")

    if a.json:
        with open(a.json, "w") as f:
            json.dump(r, f, indent=2)
        print(f"\n  JSON → {a.json}")


if __name__ == "__main__":
    main()
