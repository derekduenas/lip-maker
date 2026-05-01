#!/usr/bin/env python3
"""PM settlement watcher — mirror Kalshi settlement_log infrastructure.

Polls PM portfolio.positions() every cron firing. When a position has
`expired=True`, captures the realized PnL and writes to pm_settlements.

For each settled position:
  - market_slug, settled_at, position_qty (signed: +long, -short)
  - cost_basis_usd, realized_usd
  - rebate_earned_usd (looked up from pm_payouts via best-effort match)
  - net_outcome_usd (realized + rebate)

Idempotent: UNIQUE on (market_slug, settled_at) so re-runs don't duplicate.

USAGE:
  python pm_settlement_watcher.py             # check + log new settlements
  python pm_settlement_watcher.py --backfill  # also process historical
  python pm_settlement_watcher.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PM_ROOT = Path("/root/polymarket-maker")
sys.path.insert(0, str(PM_ROOT))

from execution.pm_auth import _load_dotenv_simple
_load_dotenv_simple(str(PM_ROOT / ".env"))
from polymarket_us import PolymarketUS

PM_DB = str(PM_ROOT / "data" / "polymarket_maker.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_settlements (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug        TEXT NOT NULL,
    settled_at         TEXT NOT NULL,
    net_position       REAL NOT NULL,
    cost_basis_usd     REAL NOT NULL,
    realized_usd       REAL NOT NULL,
    rebate_earned_usd  REAL DEFAULT 0,
    net_outcome_usd    REAL NOT NULL,
    recorded_at        TEXT NOT NULL,
    UNIQUE(market_slug, settled_at)
);
CREATE INDEX IF NOT EXISTS idx_pm_settle_slug ON pm_settlements(market_slug);
CREATE INDEX IF NOT EXISTS idx_pm_settle_time ON pm_settlements(settled_at);
"""


def get_client() -> PolymarketUS:
    return PolymarketUS(
        key_id=os.getenv("PM_API_KEY_ID"),
        secret_key=(PM_ROOT / "config" / "polymarket_secret_key.b64").read_text().strip(),
    )


def init_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA.strip().split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()


def lookup_rebate(conn: sqlite3.Connection, slug: str, settled_at: str) -> float:
    """Best-effort: sum payouts for this slug within ±2 days of settlement."""
    try:
        r = conn.execute("""
            SELECT COALESCE(SUM(payout_usd), 0)
            FROM pm_payouts
            WHERE market_slug = ?
              AND date(snapshot_at) BETWEEN date(?, '-2 days') AND date(?, '+14 days')
        """, (slug, settled_at, settled_at)).fetchone()
        return float(r[0] or 0)
    except Exception:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="include all expired positions, not just newly-detected")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    out: dict = {"ts": now.isoformat(), "new_settlements": 0, "details": []}

    try:
        c = get_client()
        p = c.portfolio.positions()
        positions = p.get("positions", {})
        if not isinstance(positions, dict):
            out["error"] = "unexpected positions response"
            print(json.dumps(out) if a.json else f"🔴 {out['error']}")
            return 2
    except Exception as e:
        out["error"] = f"position fetch: {str(e)[:120]}"
        print(json.dumps(out) if a.json else f"🔴 {out['error']}")
        return 2

    conn = sqlite3.connect(PM_DB, timeout=10.0)
    init_schema(conn)

    try:
        for slug, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            expired = pos.get("expired", False)
            if not expired and not a.backfill:
                continue

            try:
                net_position = float(pos.get("netPosition", 0))
                cost_obj = pos.get("cost", {})
                realized_obj = pos.get("realized", {})
                cost_basis = float(cost_obj.get("value", 0)) if isinstance(cost_obj, dict) else 0.0
                realized = float(realized_obj.get("value", 0)) if isinstance(realized_obj, dict) else 0.0
                settled_at = pos.get("updateTime", "") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                continue

            # Skip if zero-position fluff (never traded)
            if net_position == 0 and cost_basis == 0 and realized == 0:
                continue

            rebate = lookup_rebate(conn, slug, settled_at)
            net_outcome = realized + rebate

            try:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO pm_settlements
                    (market_slug, settled_at, net_position, cost_basis_usd,
                     realized_usd, rebate_earned_usd, net_outcome_usd, recorded_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (slug, settled_at, net_position, round(cost_basis, 4),
                      round(realized, 4), round(rebate, 4),
                      round(net_outcome, 4),
                      now.strftime("%Y-%m-%dT%H:%M:%SZ")))
                if cur.rowcount > 0:
                    out["new_settlements"] += 1
                    out["details"].append({
                        "slug":      slug,
                        "position":  net_position,
                        "cost":      round(cost_basis, 2),
                        "realized":  round(realized, 2),
                        "rebate":    round(rebate, 2),
                        "net":       round(net_outcome, 2),
                    })
            except Exception as e:
                out.setdefault("write_errors", []).append(f"{slug}: {str(e)[:60]}")
        conn.commit()

        # Roll up totals for the run
        r = conn.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(realized_usd),0),
                   COALESCE(SUM(rebate_earned_usd),0),
                   COALESCE(SUM(net_outcome_usd),0)
            FROM pm_settlements
            WHERE datetime(settled_at) > datetime('now','-7 days')
        """).fetchone()
        out["7d_count"]    = r[0]
        out["7d_realized"] = round(r[1], 2)
        out["7d_rebate"]   = round(r[2], 2)
        out["7d_net"]      = round(r[3], 2)
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        if out["new_settlements"]:
            print(f"📝 {out['new_settlements']} new PM settlements logged:")
            for d in out["details"][:10]:
                print(f"  {d['slug'][:50]:<50} pos={d['position']:>4} "
                      f"cost=${d['cost']:>5.2f} real=${d['realized']:>5.2f} "
                      f"rebate=${d['rebate']:>5.2f} → net=${d['net']:>5.2f}")
        else:
            print(f"✅ no new PM settlements (7d total: {out['7d_count']} markets, "
                  f"net=${out['7d_net']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
