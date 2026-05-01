#!/usr/bin/env python3
"""Per-series fill rate tracker (cross-venue).

Shows, for each series we've quoted in last N hours:
  - n_resting_orders we placed
  - n_filled (got hit)
  - fill_rate = filled / resting
  - avg_fill_price (where filled)
  - cumulative_volume

Helps tune which series are getting market interest vs dead-quoting.

USAGE:
  python fill_rate_tracker.py             # last 24h, both venues
  python fill_rate_tracker.py --hours 6
  python fill_rate_tracker.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

LIP_DB = "/root/lip-maker/data/lip_maker.db"
PM_DB  = "/root/polymarket-maker/data/polymarket_maker.db"


def kalshi_fill_rate(hours: int) -> list[dict]:
    """Per-series fill metrics from Kalshi DB."""
    conn = sqlite3.connect(LIP_DB, timeout=10.0)
    try:
        rows = conn.execute(f"""
            WITH series_quotes AS (
                SELECT substr(market_ticker, 1, instr(market_ticker || '-', '-') - 1) AS prefix,
                       COUNT(*) AS n_quotes,
                       SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) AS n_filled
                FROM quotes
                WHERE datetime(placed_at) > datetime('now', '-{hours} hours')
                GROUP BY prefix
            ),
            series_fills AS (
                SELECT substr(ticker, 1, instr(ticker || '-', '-') - 1) AS prefix,
                       COUNT(*) AS n_real_fills,
                       SUM(COALESCE(count_real, count)) AS contracts,
                       AVG(CASE WHEN side='yes' THEN yes_price_cents ELSE no_price_cents END) AS avg_fill_cents
                FROM fill_ledger
                WHERE datetime(created_at) > datetime('now', '-{hours} hours')
                GROUP BY prefix
            )
            SELECT q.prefix, q.n_quotes, q.n_filled,
                   COALESCE(f.n_real_fills, 0) AS api_fills,
                   COALESCE(f.contracts, 0) AS contracts,
                   COALESCE(f.avg_fill_cents, 0) AS avg_px
            FROM series_quotes q
            LEFT JOIN series_fills f USING (prefix)
            WHERE q.n_quotes > 0
            ORDER BY q.n_filled DESC, q.n_quotes DESC
            LIMIT 25
        """).fetchall()
    finally:
        conn.close()
    out = []
    for prefix, n_quotes, n_filled, api_fills, contracts, avg_px in rows:
        rate = (n_filled / n_quotes * 100) if n_quotes else 0
        out.append({
            "venue":     "kalshi",
            "series":    prefix or "?",
            "n_quotes":  n_quotes,
            "n_filled":  n_filled,
            "api_fills": api_fills,
            "contracts": round(contracts, 2),
            "avg_px":    round(avg_px, 1),
            "fill_rate_pct": round(rate, 1),
        })
    return out


def pm_fill_rate(hours: int) -> list[dict]:
    """Per-series fill metrics from PM (slug → series prefix)."""
    conn = sqlite3.connect(PM_DB, timeout=10.0)
    try:
        # PM logs orders in pm_orders, fills in pm_fill_ledger
        # Series prefix = first 3 segments of slug (e.g. "tc-temp-laxhigh")
        try:
            rows = conn.execute(f"""
                WITH series_quotes AS (
                    SELECT substr(market_slug, 1,
                            CASE
                                WHEN instr(substr(market_slug, instr(market_slug,'-')+1) || '-', '-') > 0
                                THEN instr(market_slug,'-') + instr(substr(market_slug, instr(market_slug,'-')+1) || '-', '-')
                                ELSE length(market_slug)
                            END) AS prefix,
                            COUNT(*) AS n
                    FROM pm_orders
                    WHERE datetime(placed_at) > datetime('now', '-{hours} hours')
                    GROUP BY prefix
                )
                SELECT prefix, n FROM series_quotes WHERE n > 0
                ORDER BY n DESC LIMIT 15
            """).fetchall()
        except Exception:
            rows = []
        # Fills from pm_fill_ledger if exists
        try:
            fill_rows = conn.execute(f"""
                SELECT market_slug, COUNT(*) n, SUM(quantity) qty
                FROM pm_fill_ledger
                WHERE datetime(filled_at) > datetime('now', '-{hours} hours')
                GROUP BY market_slug
                ORDER BY n DESC LIMIT 15
            """).fetchall()
        except Exception:
            fill_rows = []
    finally:
        conn.close()

    out = []
    for prefix, n in rows:
        out.append({
            "venue": "pm", "series": prefix or "?",
            "n_quotes": n, "n_filled": 0, "fill_rate_pct": 0,
        })
    for slug, n, qty in fill_rows:
        out.append({
            "venue": "pm", "series": (slug or "?")[:30],
            "n_filled": n, "contracts": float(qty or 0),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "hours":  a.hours,
        "kalshi": [],
        "pm":     [],
    }
    try:
        out["kalshi"] = kalshi_fill_rate(a.hours)
    except Exception as e:
        out["kalshi_err"] = str(e)[:120]
    try:
        out["pm"] = pm_fill_rate(a.hours)
    except Exception as e:
        out["pm_err"] = str(e)[:120]

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n📊 FILL RATE BY SERIES (last {a.hours}h)")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"\n🟢 KALSHI:")
        if out.get("kalshi_err"):
            print(f"  🔴 {out['kalshi_err']}")
        else:
            print(f"  {'series':<28} {'quoted':>7} {'filled':>7} {'fill%':>6} {'contracts':>10} {'avg_px':>7}")
            for k in out["kalshi"][:15]:
                print(f"  {k['series']:<28} {k['n_quotes']:>7d} {k['n_filled']:>7d} "
                      f"{k['fill_rate_pct']:>5.1f}% {k.get('contracts', 0):>9.1f}c "
                      f"{k.get('avg_px', 0):>6.0f}c")
        print(f"\n🟡 POLYMARKET:")
        if out.get("pm_err"):
            print(f"  🔴 {out['pm_err']}")
        else:
            for p in out["pm"][:15]:
                print(f"  {p.get('series', '?'):<40} q={p.get('n_quotes', 0):>4} "
                      f"f={p.get('n_filled', 0):>4}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
