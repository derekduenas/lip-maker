"""LIP opportunity scanner — fresh API pull + ranked by total period reward.

Equivalent to scrolling kalshi.com/incentives but programmatic. Use to:
  1. Refresh stale discovery (Kalshi adds new markets daily)
  2. Identify high-pool markets we're MISSING due to filters/blocklist
  3. Rank by TOTAL period reward (UI view) not just per-day

Run anytime — read-only, no quoting changes:
  python tools/lip_opportunity_scanner.py                 # top 30
  python tools/lip_opportunity_scanner.py --top 100       # all the high-tier
  python tools/lip_opportunity_scanner.py --missing-only  # only show markets we're NOT on
  python tools/lip_opportunity_scanner.py --series KXNBA  # filter by series prefix
  python tools/lip_opportunity_scanner.py --refresh       # call discover() to refresh DB first
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_auth import KalshiClient


def fetch_all_active_lip(client: KalshiClient) -> list[dict]:
    """Pull every active liquidity-incentive program directly from Kalshi.
    Same data backing the kalshi.com/incentives page."""
    out: list[dict] = []
    cursor = None
    while True:
        params = {"status": "active", "type": "liquidity", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.get_unauth("/incentive_programs", params=params)
        except Exception as e:
            print(f"  /incentive_programs failed: {e}")
            break
        for raw in resp.get("incentive_programs", []):
            if raw.get("incentive_type") != "liquidity":
                continue
            out.append(raw)
        cursor = resp.get("next_cursor")
        if not cursor:
            break
    return out


def parse_program(raw: dict) -> dict:
    """Parse Kalshi v2 incentive_program response.

    Verified field names from live API response 2026-04-27:
      period_reward (cents int), target_size_fp (str), discount_factor_bps (int)
      series_ticker NOT in response — derive from market_ticker prefix.
    """
    start = raw.get("start_date", "")
    end = raw.get("end_date", "")
    # Kalshi unit: CENTI-CENTS = divide by 10,000 (per engine/lip_discovery)
    period_reward_usd = float(raw.get("period_reward", 0)) / 10_000.0
    market_ticker = raw.get("market_ticker", "")
    # Derive series from ticker (everything before first dash)
    series_ticker = market_ticker.split("-", 1)[0] if market_ticker else ""
    days = 1.0
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        days = max(1.0, (e - s).total_seconds() / 86400)
    except Exception:
        pass
    reward_per_day = period_reward_usd / days
    try:
        target_size = int(float(raw.get("target_size_fp", 0)))
    except (TypeError, ValueError):
        target_size = 0
    return {
        "id":               raw.get("id", ""),
        "market_ticker":    market_ticker,
        "series_ticker":    series_ticker,
        "start_date":       start,
        "end_date":         end,
        "period_reward":    period_reward_usd,
        "reward_per_day":   reward_per_day,
        "days":             round(days, 2),
        "target_size":      target_size,
        "discount_factor":  float(raw.get("discount_factor_bps", 5000)) / 10_000.0,
        "paid_out":         bool(raw.get("paid_out", False)),
    }


def already_in_db(market_tickers: list[str], db_path: str) -> set[str]:
    """Return the set of tickers we already have in lip_programs."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            f"SELECT market_ticker FROM lip_programs WHERE market_ticker IN ({','.join('?'*len(market_tickers))})",
            market_tickers,
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def enrolled_status(market_tickers: list[str], db_path: str) -> dict[str, dict]:
    """Per-ticker: enrolled flag + blocked_reason if any."""
    if not market_tickers:
        return {}
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            f"SELECT market_ticker, enrolled, blocked_reason FROM lip_programs WHERE market_ticker IN ({','.join('?'*len(market_tickers))})",
            market_tickers,
        ).fetchall()
        return {r[0]: {"enrolled": r[1], "reason": r[2]} for r in rows}
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--missing-only", action="store_true",
                   help="Show only markets NOT in our DB (truly new opportunities)")
    p.add_argument("--blocked-only", action="store_true",
                   help="Show markets we SAW but BLOCKED by filter — review for unblock")
    p.add_argument("--series", default=None,
                   help="Filter by series prefix (e.g., KXNBA, KXMEX)")
    p.add_argument("--min-period-reward", type=float, default=20.0,
                   help="Minimum TOTAL period reward USD (default $20)")
    a = p.parse_args()

    print(f"\n{'═'*100}")
    print(f"LIP OPPORTUNITY SCANNER — fresh pull from Kalshi /incentive_programs")
    print(f"{'═'*100}\n")

    client = KalshiClient()
    print("Fetching live programs from Kalshi…")
    raw_programs = fetch_all_active_lip(client)
    print(f"  {len(raw_programs)} active liquidity programs returned\n")

    parsed = [parse_program(r) for r in raw_programs]
    parsed = [p for p in parsed if p["period_reward"] >= a.min_period_reward
              and not p["paid_out"]]

    if a.series:
        parsed = [p for p in parsed if p["series_ticker"].startswith(a.series)]
        print(f"  filtered to series prefix '{a.series}': {len(parsed)} markets\n")

    # Sort by total period reward DESC (UI view)
    parsed.sort(key=lambda x: -x["period_reward"])

    # Cross-check against our DB
    tickers = [p["market_ticker"] for p in parsed]
    in_db = already_in_db(tickers, settings.DB_PATH)
    statuses = enrolled_status(list(in_db), settings.DB_PATH)

    # Filter views
    if a.missing_only:
        parsed = [p for p in parsed if p["market_ticker"] not in in_db]
        print(f"  MISSING-ONLY view: {len(parsed)} markets not yet in our DB\n")
    if a.blocked_only:
        parsed = [p for p in parsed
                  if p["market_ticker"] in statuses
                  and statuses[p["market_ticker"]]["enrolled"] == 0]
        print(f"  BLOCKED-ONLY view: {len(parsed)} markets we saw but blocked\n")

    if not parsed:
        print("  No markets match filters.")
        return 0

    print(f"{'TOP':<4} {'Market':<48s} {'Series':<14s} {'Reward':>8s} {'$/day':>7s} "
          f"{'Days':>5s} {'Status':<25s}")
    print(f"{'-'*4} {'-'*48} {'-'*14} {'-'*8} {'-'*7} {'-'*5} {'-'*25}")

    for i, p in enumerate(parsed[:a.top], 1):
        ticker = p["market_ticker"][:48]
        series = (p["series_ticker"] or "")[:14]
        reward = p["period_reward"]
        per_day = p["reward_per_day"]
        days = p["days"]
        status = ""
        if p["market_ticker"] not in in_db:
            status = "🆕 NEW (not in DB)"
        else:
            s = statuses[p["market_ticker"]]
            if s["enrolled"]:
                status = "✅ enrolled"
            else:
                status = f"❌ {(s['reason'] or 'blocked')[:20]}"
        print(f"{i:>3}. {ticker:<48s} {series:<14s} ${reward:>7.0f} ${per_day:>6.2f} "
              f"{days:>5.1f} {status:<25s}")

    # Summary
    n_new = sum(1 for p in parsed[:a.top] if p["market_ticker"] not in in_db)
    n_blocked = sum(1 for p in parsed[:a.top]
                    if p["market_ticker"] in statuses
                    and statuses[p["market_ticker"]]["enrolled"] == 0)
    n_enrolled = sum(1 for p in parsed[:a.top]
                     if p["market_ticker"] in statuses
                     and statuses[p["market_ticker"]]["enrolled"])

    print(f"\n{'─'*100}")
    print(f"SUMMARY (top {a.top} by total period reward):")
    print(f"  ✅ enrolled (we're on these):       {n_enrolled}")
    print(f"  ❌ in DB but blocked by our filter: {n_blocked}")
    print(f"  🆕 NEW (need refresh discovery):    {n_new}")
    print(f"\n  Total potential pool (top {a.top}): "
          f"${sum(p['period_reward'] for p in parsed[:a.top]):,.0f}")
    print(f"  Per-day equivalent: "
          f"${sum(p['reward_per_day'] for p in parsed[:a.top]):,.2f}/day")

    if n_new > 0:
        print(f"\n  💡 Run discovery to add the {n_new} new markets:")
        print(f"     python -m engine.lip_discovery")
    if n_blocked > 0:
        print(f"\n  💡 Review blocked markets to see if filters need tuning:")
        print(f"     python tools/lip_opportunity_scanner.py --blocked-only --top {a.top}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
