"""PM fill sync — pulls trade activities from PM and writes to
pm_fill_ledger. Without this, we have NO PnL attribution at the
fill level. Mirrors the Kalshi fills_sync.py pattern.

USAGE:
  python pm_fills_sync.py            # one-shot sync
  python pm_fills_sync.py --json

CRON: every 60s (PM activities API tolerates frequent polling).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/root/polymarket-maker')
from execution.pm_auth import _load_dotenv_simple
_load_dotenv_simple('/root/polymarket-maker/.env')
from polymarket_us import PolymarketUS

DB = "/root/polymarket-maker/data/polymarket_maker.db"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pm_fill_ledger (
            trade_id    TEXT,
            order_id    TEXT,
            market_slug TEXT,
            side        TEXT,
            count       INTEGER,
            price       REAL,
            is_taker    INTEGER,
            created_at  TEXT,
            synced_at   TEXT,
            PRIMARY KEY (trade_id, market_slug)
        );
        CREATE INDEX IF NOT EXISTS idx_pm_fill_ledger_slug ON pm_fill_ledger(market_slug);
        CREATE INDEX IF NOT EXISTS idx_pm_fill_ledger_created ON pm_fill_ledger(created_at);
    """)
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()

    out: dict = {"ts": datetime.now(timezone.utc).isoformat()}

    c = PolymarketUS(
        key_id=os.getenv("PM_API_KEY_ID"),
        secret_key=open("/root/polymarket-maker/config/polymarket_secret_key.b64").read().strip(),
    )
    try:
        r = c.portfolio.activities({"limit": a.limit})
        acts = r.get("activities", []) if isinstance(r, dict) else []
    except Exception as e:
        out["error"] = str(e)[:200]
        if a.json: print(json.dumps(out, indent=2))
        else: print(f"🔴 activities fetch failed: {e}")
        return 1

    out["activities_pulled"] = len(acts)

    conn = sqlite3.connect(DB, timeout=10.0)
    _ensure_schema(conn)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    skipped = 0

    for x in acts:
        if not isinstance(x, dict): continue
        atype = x.get("type", "")
        # We care about TRADE events (actual fills) — POSITION_RESOLUTION is settlement
        if "TRADE" not in atype: continue

        trade = x.get("trade", {}) or {}
        slug = trade.get("marketSlug", "")
        if not slug: continue

        trade_id = trade.get("tradeId") or x.get("id", "")
        order_id = trade.get("orderId", "")
        side = trade.get("side", trade.get("intent", ""))[:12]
        qty = int(float(trade.get("quantity", 0) or 0))
        price = float((trade.get("price", {}) or {}).get("value", 0) or 0)
        is_taker = 1 if trade.get("isTaker") else 0
        created_at = trade.get("createTime", x.get("createTime", ""))

        if not trade_id:
            skipped += 1
            continue

        try:
            conn.execute(
                "INSERT OR IGNORE INTO pm_fill_ledger "
                "(trade_id, order_id, market_slug, side, count, price, "
                "is_taker, created_at, synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (trade_id, order_id, slug, side, qty, price, is_taker,
                 created_at, now_iso),
            )
            if conn.total_changes > 0 and conn.execute(
                "SELECT changes()").fetchone()[0] > 0:
                inserted += 1
        except Exception:
            skipped += 1
    conn.commit()
    conn.close()

    out["inserted"] = inserted
    out["skipped"]  = skipped

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n━━━ PM FILL SYNC ━━━")
        print(f"  Activities pulled: {out['activities_pulled']}")
        print(f"  Inserted:          {inserted}")
        print(f"  Skipped:           {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
