#!/usr/bin/env python3
"""scan_hedge_opps.py — find Kalshi LIP markets we can hedge across venues.

Scans active Kalshi LIP markets and categorizes each by available hedge:
  - Kraken (crypto monthly: BTC, ETH, XRP, SOL, ADA, DOGE, ZEC)
  - CME via IBKR (Fed, CPI, commodity, indices)
  - Polymarket US (political, weather, event 1:1 cross-venue)
  - Hyperliquid (HYPE, no adapter yet)
  - Unhedgeable

For each hedgeable market it shows:
  - LIP daily pool size
  - Per-series learned net-capture (or 0.25 fallback)
  - Expected daily $ (pool × net_calib)
  - Hedge instrument + venue

USAGE
  python tools/scan_hedge_opps.py             # human-readable
  python tools/scan_hedge_opps.py --top 30    # top N hedgeable
  python tools/scan_hedge_opps.py --venue Kraken
  python tools/scan_hedge_opps.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "/root/lip-maker/data/lip_maker.db"


def fetch_active_lip() -> list[dict]:
    """Pull every active LIP market with its current pool size."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = conn.execute("""
            SELECT market_ticker, reward_per_day_usd,
                   target_size, discount_factor, end_date
            FROM lip_programs
            WHERE end_date > datetime('now')
              AND reward_per_day_usd >= 1.0
            ORDER BY reward_per_day_usd DESC
        """).fetchall()
    finally:
        conn.close()
    return [
        {"ticker": r[0], "pool_per_day": r[1], "target_size": r[2],
         "discount_factor": r[3], "end_date": r[4]}
        for r in rows
    ]


def get_calib(ticker: str) -> tuple[float, int]:
    """Return (net_calib, n_samples). Falls back to (0.25, 0) on cold start."""
    from engine.calibration_ewma import series_prefix
    prefix = series_prefix(ticker)
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3.0)
        row = conn.execute(
            "SELECT calibration, n_samples FROM market_calibration WHERE key=?",
            (prefix,),
        ).fetchone()
        conn.close()
        if row and int(row[1]) >= 5:
            return float(row[0]), int(row[1])
    except Exception:
        pass
    return 0.25, 0


def categorize_market(ticker: str) -> tuple[str, str | None]:
    """Return (category, instrument). Category is one of:
       Kraken, CME, ICE, Polymarket, Hyperliquid, NO_HEDGE_SERIES, unhedgeable."""
    from cross_venue.market_match import (
        hedge_for_ticker, is_explicit_no_hedge,
    )
    if is_explicit_no_hedge(ticker):
        return ("NO_HEDGE_SERIES", None)
    spec = hedge_for_ticker(ticker)
    if spec is None:
        return ("unhedgeable", None)
    return (spec.hedge_venue, spec.instrument)


def scan(top_n: int | None = None,
         venue_filter: str | None = None) -> dict:
    markets = fetch_active_lip()
    by_venue: dict[str, list] = defaultdict(list)
    venue_pool: dict[str, float] = defaultdict(float)
    venue_expected: dict[str, float] = defaultdict(float)
    total_pool = 0.0
    total_expected = 0.0

    for m in markets:
        ticker = m["ticker"]
        pool = m["pool_per_day"]
        total_pool += pool

        calib, n_samples = get_calib(ticker)
        expected = pool * calib
        total_expected += expected

        venue, instrument = categorize_market(ticker)
        entry = {
            "ticker": ticker,
            "pool_per_day": round(pool, 2),
            "net_calib": round(calib, 4),
            "n_samples": n_samples,
            "expected_per_day": round(expected, 2),
            "instrument": instrument,
        }
        by_venue[venue].append(entry)
        venue_pool[venue] += pool
        venue_expected[venue] += expected

    # Sort each bucket by expected daily $
    for v in by_venue:
        by_venue[v].sort(key=lambda x: -x["expected_per_day"])
        if top_n:
            by_venue[v] = by_venue[v][:top_n]

    if venue_filter:
        by_venue = {venue_filter: by_venue.get(venue_filter, [])}

    return {
        "total_markets": len(markets),
        "total_pool_per_day": round(total_pool, 2),
        "total_expected_per_day": round(total_expected, 2),
        "by_venue_pool": {k: round(v, 2) for k, v in venue_pool.items()},
        "by_venue_expected": {k: round(v, 2) for k, v in venue_expected.items()},
        "markets_by_venue": dict(by_venue),
    }


# ── Rendering ────────────────────────────────────────────────────────────────

GREEN = "\033[92m"; YELLOW = "\033[93m"; RED = "\033[91m"
GRAY = "\033[90m"; BOLD = "\033[1m"; RESET = "\033[0m"

VENUE_ORDER = ["Kraken", "CME", "Polymarket", "Hyperliquid", "ICE",
               "NO_HEDGE_SERIES", "unhedgeable"]


def render(result: dict, top_n: int | None) -> None:
    print()
    print(f"{BOLD}=== HEDGE OPPORTUNITY SCAN — {result['total_markets']} active LIP markets ==={RESET}")
    print()
    print(f"{BOLD}TOTALS{RESET}")
    print(f"  total LIP pool:          ${result['total_pool_per_day']:>10,.0f}/day")
    print(f"  expected income (calib): ${result['total_expected_per_day']:>10,.0f}/day")
    print()
    print(f"{BOLD}BY VENUE — addressable pool + expected income{RESET}")
    by_pool = result["by_venue_pool"]
    by_exp = result["by_venue_expected"]
    for v in VENUE_ORDER:
        if v not in by_pool:
            continue
        pool = by_pool[v]
        exp = by_exp.get(v, 0)
        pct_total = (pool / result["total_pool_per_day"] * 100) if result["total_pool_per_day"] else 0
        color = ""
        if v in ("Kraken", "CME", "Polymarket"):
            color = GREEN
        elif v == "Hyperliquid":
            color = YELLOW
        elif v in ("NO_HEDGE_SERIES", "unhedgeable", "ICE"):
            color = GRAY
        n = len(result["markets_by_venue"].get(v, []))
        print(f"  {color}{v:18s}{RESET}  pool=${pool:>9,.0f}/d  expected=${exp:>8,.0f}/d  ({pct_total:>4.1f}% of pool, {n} markets)")

    # Per-venue detail
    print()
    print(f"{BOLD}TOP HEDGEABLE OPPORTUNITIES (sorted by expected $/day){RESET}")
    for v in ("Kraken", "CME", "Polymarket", "Hyperliquid"):
        if v not in result["markets_by_venue"]:
            continue
        markets = result["markets_by_venue"][v]
        if not markets:
            continue
        print()
        color = GREEN if v in ("Kraken", "CME", "Polymarket") else YELLOW
        n = top_n if top_n else 10
        print(f"{color}── {v} ──{RESET}  (showing top {min(n, len(markets))} of {len(markets)})")
        print(f"  {'ticker':<55s} {'pool$/d':>8s} {'net_calib':>10s} {'n_samp':>7s} {'exp$/d':>8s} {'instr':<12s}")
        for m in markets[:n]:
            calib_str = f"{m['net_calib']:.4f}" if m['n_samples'] >= 5 else f"({m['net_calib']:.2f})"
            print(f"  {m['ticker']:<55s} {m['pool_per_day']:>8.2f} {calib_str:>10s} "
                  f"{m['n_samples']:>7d} {m['expected_per_day']:>8.2f} {m['instrument'] or '':<12s}")

    # Summary
    hedgeable_pool = sum(by_pool.get(v, 0) for v in ("Kraken", "CME", "Polymarket"))
    hedgeable_exp = sum(by_exp.get(v, 0) for v in ("Kraken", "CME", "Polymarket"))
    print()
    print(f"{BOLD}HEDGEABLE COVERAGE{RESET}")
    pct_pool = (hedgeable_pool / result["total_pool_per_day"] * 100) if result["total_pool_per_day"] else 0
    pct_exp = (hedgeable_exp / result["total_expected_per_day"] * 100) if result["total_expected_per_day"] else 0
    print(f"  pool:     ${hedgeable_pool:>9,.0f}/d  ({pct_pool:.1f}% of total)")
    print(f"  expected: ${hedgeable_exp:>9,.0f}/d  ({pct_exp:.1f}% of total)")
    print()
    print(f"  {GRAY}(values in '()' on net_calib means cold-start fallback 0.25 — needs more settlements){RESET}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=10,
                   help="show top N markets per venue (default 10)")
    p.add_argument("--venue", choices=["Kraken", "CME", "Polymarket",
                                       "Hyperliquid", "ICE",
                                       "NO_HEDGE_SERIES", "unhedgeable"],
                   help="restrict output to one venue")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    result = scan(top_n=None, venue_filter=a.venue)
    if a.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        render(result, a.top)


if __name__ == "__main__":
    main()
