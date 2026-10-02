"""Stranded Inventory Liquidator — predator-mode exit logic.

PROBLEM (2026-05-01):
  We hold 96 positions but only 2-6 active resting orders. 95 positions
  are STRANDED — markets we dropped from quoting (top-N pruning, runtime
  blacklist, expiry approach) where inventory just sits accumulating
  mark-to-market drift. Active inventory skew (#97) only fires inside
  the active quote loop, so it can't help dropped markets.

  24h fill audit: 19 fills, 0 exits. Pure buffalo behavior.

DEFINITION (stranded position):
  - We hold non-zero position
  - No active resting order for the market
  - Market still tradeable (status != 'closed')
  - Market not within SKIP_NEAR_SETTLE_MIN of close (settlement handles)
  - Market not in runtime blacklist (we blocked for a reason)

ACTION:
  Post a single SELL order at midpoint − 1 tick on our long side.
  - Long YES (position > 0): sell yes at (best_yes_bid)
  - Short YES / Long NO (position < 0): sell no at (best_no_bid)
  Use post-only to never cross. If unfilled in 10 minutes, retry at -1c.

USAGE:
  python stranded_liquidator.py            # audit only (default — safe)
  python stranded_liquidator.py --live     # actually post exit orders
  python stranded_liquidator.py --json     # machine-readable

CRON: every 60-120s when --live to maintain pressure on stranded book.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent))

from execution.kalshi_auth import KalshiClient
from config import settings

LIP_DB = "/root/lip-maker/data/lip_maker.db"
SKIP_NEAR_SETTLE_MIN = 30   # don't liquidate within 30min of close — settlement handles
SKIP_FAR_FROM_SETTLE_HOURS = 720  # skip positions > 30d out (long-dated political can ride)
MIN_POSITION_TO_LIQ = 1     # ignore dust positions
EXIT_TICK_OFFSET = 0        # 0 = join best, -1 = inside, +1 = behind (price ladder)


SCHEMA = """
CREATE TABLE IF NOT EXISTS stranded_liq_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    position        REAL NOT NULL,
    side_to_sell    TEXT NOT NULL,
    exit_price_c    INTEGER,
    order_id        TEXT,
    status          TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_stranded_liq_ticker ON stranded_liq_log(ticker);
CREATE INDEX IF NOT EXISTS idx_stranded_liq_time   ON stranded_liq_log(detected_at);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _get_blacklist(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT ticker FROM market_blacklist "
        "WHERE expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    ).fetchall()
    return {r[0] for r in rows}


def _market_status(c: KalshiClient, ticker: str) -> dict:
    """Returns {status, close_time, best_yes_bid, best_no_bid} or {}."""
    try:
        m = c.get(f"/markets/{ticker}").get("market", {})
        return {
            "status":       m.get("status", ""),
            "close_time":   m.get("close_time", ""),
            "yes_bid":      int(m.get("yes_bid", 0) or 0),
            "yes_ask":      int(m.get("yes_ask", 0) or 0),
            "no_bid":       int(m.get("no_bid", 0) or 0),
            "no_ask":       int(m.get("no_ask", 0) or 0),
        }
    except Exception:
        return {}


def _post_sell(c: KalshiClient, ticker: str, side: str, price_c: int, count: int) -> tuple[str, str]:
    """Post a sell order. Returns (order_id, status_msg)."""
    coid = f"LIQ-{uuid.uuid4().hex[:12]}"
    if price_c <= 0 or price_c >= 100:
        return ("", f"skip: edge price {price_c}c")
    body = {
        "ticker": ticker,
        "side":   side,
        "action": "sell",
        "type":   "limit",
        "count":  count,
        "post_only": True,
        "client_order_id": coid,
    }
    if side == "yes":
        body["yes_price"] = price_c
    else:
        body["no_price"] = price_c
    try:
        resp = c.post("/portfolio/orders", body)
        oid = resp.get("order", {}).get("order_id", "")
        return (oid, "posted")
    except Exception as e:
        return ("", f"reject: {str(e)[:80]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually post exit orders")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out: dict = {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "mode":       "LIVE" if a.live else "AUDIT",
        "evaluated":  0,
        "skipped":    [],
        "would_exit": [],
        "executed":   [],
    }

    c = KalshiClient()
    positions = c.get("/portfolio/positions", params={"limit": 200}).get("market_positions", [])
    orders    = c.get("/portfolio/orders", params={"status": "resting", "limit": 200}).get("orders", [])
    resting_tickers = {o.get("ticker") for o in orders}

    active_pos = [p for p in positions if abs(float(p.get("position_fp", 0))) >= MIN_POSITION_TO_LIQ]

    conn = sqlite3.connect(LIP_DB, timeout=10.0)
    _ensure_schema(conn)
    blacklist = _get_blacklist(conn)
    now_utc = datetime.now(timezone.utc)

    for p in active_pos:
        out["evaluated"] += 1
        ticker = p.get("ticker", "")
        pos    = float(p.get("position_fp", 0))

        # Skip if has resting order (recirculation handles it)
        if ticker in resting_tickers:
            out["skipped"].append({"ticker": ticker, "reason": "has_resting_order"})
            continue
        # Skip if blacklisted (we blocked for a reason)
        if ticker in blacklist:
            out["skipped"].append({"ticker": ticker, "reason": "in_blacklist"})
            continue

        m = _market_status(c, ticker)
        if not m:
            out["skipped"].append({"ticker": ticker, "reason": "market_lookup_failed"})
            continue
        if m["status"] != "active":
            out["skipped"].append({"ticker": ticker, "reason": f"status={m['status']}"})
            continue

        # Skip if too close to settlement (settlement handles)
        ct = m.get("close_time", "")
        if ct:
            try:
                close_dt = datetime.strptime(ct[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                mins_to_close = (close_dt - now_utc).total_seconds() / 60
                if 0 < mins_to_close < SKIP_NEAR_SETTLE_MIN:
                    out["skipped"].append({"ticker": ticker, "reason": f"settles_in_{int(mins_to_close)}min"})
                    continue
                if mins_to_close > SKIP_FAR_FROM_SETTLE_HOURS * 60:
                    out["skipped"].append({"ticker": ticker, "reason": f"too_far_out_{int(mins_to_close/60)}h"})
                    continue
            except Exception:
                pass

        # Determine sell side + price
        # Long YES → sell yes at yes_bid (best bid we hit by selling)
        # Long NO  → sell no at no_bid
        if pos > 0:
            side = "yes"
            price_c = m["yes_bid"]  # lift the best bid by selling
        else:
            side = "no"
            price_c = m["no_bid"]

        if price_c <= 0:
            out["skipped"].append({"ticker": ticker, "reason": "no_bid_to_sell_into"})
            continue

        count = int(abs(pos))
        plan = {"ticker": ticker, "pos": pos, "side": side, "price_c": price_c, "count": count}

        if a.live:
            oid, status = _post_sell(c, ticker, side, price_c, count)
            plan["order_id"] = oid
            plan["status"]   = status
            conn.execute(
                "INSERT INTO stranded_liq_log (ticker, detected_at, position, "
                "side_to_sell, exit_price_c, order_id, status, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, now_utc.isoformat(), pos, side, price_c, oid, status, "live"),
            )
            conn.commit()
            out["executed"].append(plan)
        else:
            plan["status"] = "AUDIT"
            out["would_exit"].append(plan)

    conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n━━━ STRANDED INVENTORY LIQUIDATOR — {out['mode']} ━━━")
        print(f"  Evaluated: {out['evaluated']}  |  Skipped: {len(out['skipped'])}  |  "
              f"Action: {len(out['would_exit']) + len(out['executed'])}")
        if out["skipped"]:
            from collections import Counter
            cnt = Counter(s["reason"] for s in out["skipped"])
            print(f"\n  Skip reasons:")
            for reason, n in cnt.most_common():
                print(f"    {reason:<35} {n}")
        target = out["executed"] if a.live else out["would_exit"]
        if target:
            print(f"\n  {'EXIT POSTED' if a.live else 'WOULD EXIT'}:")
            print(f"  {'ticker':<50} {'pos':>6} {'side':>4} {'@':>4}c {'qty':>4}  status")
            for x in target:
                st = x.get("status", "?")[:30]
                oid = x.get("order_id", "")[:8]
                print(f"  {x['ticker'][:48]:<50} {x['pos']:>6.0f} {x['side']:>4} {x['price_c']:>4} {x['count']:>4}  {st}  {oid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
