"""Competitor density scanner — rank enrolled markets by EXPECTED NET DOLLARS.

THE PROBLEM (2026-04-28, #124 strategic shock):
  Winner-series commodity LIPs (KXBRENTD/CORNW/COPPERD/etc.) disappeared
  from Kalshi. We need to find new winners FAST. Raw reward_per_day
  ranking is broken — a $50/day market with 10 LPs (5% share) earns less
  than a $10/day market with 1 LP (50% share).

  This tool computes the EXPECTED net dollars per day for every enrolled
  market by combining three signals:

    1. OBSERVED SHARE — avg(our_score / total_score) over recent valid
       snapshots. Direct measure of pool dilution from competition.

    2. QUALIFICATION RATE — what fraction of snapshots actually qualify
       (both sides meet TargetSize). Markets where rate < 0.3 are mostly
       paying $0 even when we're at-best.

    3. ADVERSE COST — sum of negative settlements per series, divided by
       n_settlements, gives expected loss per market. Subtract from gross.

  expected_net_per_day = reward × observed_share × qual_rate
                        - adverse_cost_per_market_per_day

  Markets with no snapshot data fall back to default prior (0.20 share,
  1.0 qual rate). For NEW markets in winner series we'd whitelist
  (KXBRENTD etc.), but those are gone now.

USAGE:
  python tools/competitor_density.py                # full scan, top 30
  python tools/competitor_density.py --top 100     # full ranking
  python tools/competitor_density.py --series KX*   # filter
  python tools/competitor_density.py --quoted       # only currently-quoted

Output: ranked table of (ticker, reward, share, qual, adverse, net_per_day).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


# ── Tunables ─────────────────────────────────────────────────────────────
POST_FIX_CUTOFF      = "2026-04-25 18:14:51"
DEFAULT_SHARE_PRIOR  = 0.20    # us + 4 effective competitors
DEFAULT_QUAL_PRIOR   = 1.0     # absent data → assume reachable
MIN_SNAPS_FOR_OBS    = 10      # need this many to trust observed share
SETTLEMENT_LOOKBACK_DAYS = 7   # window for adverse cost averaging

_MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def _ticker_settled_already(ticker: str) -> bool:
    """Parse YYMMMDD or YYMMMDDHH date from ticker name. Returns True if
    the ticker's named close has already passed. Defensive: returns False
    if can't parse (don't filter unknown formats).

    Examples:
      KXBRENTD-26APR2117-T103     → Apr 21 2026 17:00 ET → close=21:00 UTC
      KXCORNW-26APR2417-T440      → Apr 24 2026 17:00 ET → close=21:00 UTC
      KXTRUMPACT-26APR26-T3       → Apr 26 2026 (no hour, treat as end of day)
    """
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})?", ticker)
    if not m:
        return False
    yy, mmm, dd, hh = m.groups()
    month = _MONTHS.get(mmm.upper())
    if not month:
        return False
    try:
        # If hour given (HH), treat as ET hour → UTC = +4 (EDT in Apr-Oct)
        # If no hour, treat as 23:59 of named day (end of day, conservative)
        if hh:
            close_utc = datetime(2000 + int(yy), month, int(dd),
                                 int(hh) + 4, tzinfo=timezone.utc)
        else:
            close_utc = datetime(2000 + int(yy), month, int(dd),
                                 23, 59, tzinfo=timezone.utc)
        return close_utc < datetime.now(timezone.utc)
    except Exception:
        return False


def scan(db_path: str = settings.DB_PATH,
         max_target_size: int = 2500) -> list[dict]:
    """Return list of dicts ranked by expected_net_per_day desc.

    Each dict has: market_ticker, series_ticker, reward_per_day_usd,
    observed_share, qual_rate, n_snaps, adverse_per_market_per_day,
    expected_gross_per_day, expected_net_per_day, competitor_count_est,
    confidence ('observed' | 'series' | 'prior').
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        # Per-market snapshot stats (post-fix only)
        market_stats = {}
        rows = conn.execute(
            """SELECT market_ticker,
                      AVG(CASE WHEN total_score > 0
                               THEN our_score * 1.0 / total_score ELSE 0 END) AS avg_share,
                      AVG(snapshot_valid * 1.0) AS qual_rate,
                      COUNT(*) AS n_snaps
               FROM lip_snapshots
               WHERE was_resting = 1
                 AND datetime(captured_at) > datetime(?)
               GROUP BY market_ticker""",
            (POST_FIX_CUTOFF,),
        ).fetchall()
        for r in rows:
            market_stats[r[0]] = {"share": r[1], "qual": r[2], "n": r[3]}

        # Per-series snapshot stats (fallback for markets w/o per-market data)
        series_stats = {}
        rows = conn.execute(
            """SELECT substr(market_ticker, 1, instr(market_ticker || '-', '-') - 1) AS series,
                      AVG(CASE WHEN total_score > 0
                               THEN our_score * 1.0 / total_score ELSE 0 END) AS avg_share,
                      AVG(snapshot_valid * 1.0) AS qual_rate,
                      COUNT(*) AS n_snaps
               FROM lip_snapshots
               WHERE was_resting = 1
                 AND datetime(captured_at) > datetime(?)
               GROUP BY series
               HAVING COUNT(*) >= 20""",
            (POST_FIX_CUTOFF,),
        ).fetchall()
        for r in rows:
            series_stats[r[0]] = {"share": r[1], "qual": r[2], "n": r[3]}

        # Per-series adverse cost from settlement_log (#120 populated this)
        adverse_stats = {}
        rows = conn.execute(
            """SELECT series_prefix,
                      COUNT(*) AS n_settled,
                      ABS(COALESCE(SUM(CASE WHEN our_realized_usd < 0
                                            THEN our_realized_usd ELSE 0 END), 0)) AS adverse_total,
                      COALESCE(SUM(rebate_earned_usd), 0) AS rebate_total
               FROM settlement_log
               WHERE datetime(close_time) > datetime('now', '-' || ? || ' days')
               GROUP BY series_prefix""",
            (SETTLEMENT_LOOKBACK_DAYS,),
        ).fetchall()
        for r in rows:
            n_settled = r[1]
            if n_settled > 0:
                adverse_stats[r[0]] = {
                    "n_settled":         n_settled,
                    "adverse_per_mkt":   r[2] / n_settled,
                    "rebate_per_mkt":    r[3] / n_settled,
                }

        # All currently-enrolled, non-blacklisted, future-end markets
        rows = conn.execute(
            """SELECT p.market_ticker, p.series_ticker, p.reward_per_day_usd,
                      p.target_size, p.discount_factor, p.end_date
               FROM lip_programs p
               LEFT JOIN market_blacklist b
                 ON p.market_ticker = b.ticker
                 AND datetime(b.expires_at) > datetime('now')
               WHERE p.enrolled = 1 AND p.paid_out = 0
                 AND p.target_size <= ?
                 AND date(p.end_date) > date('now')
                 AND b.ticker IS NULL""",
            (max_target_size,),
        ).fetchall()
    finally:
        conn.close()

    results = []
    for r in rows:
        ticker, series, reward, target, df, end_date = r
        if not reward:
            continue
        # Skip markets whose ticker-named close has already passed.
        # The lip_programs.end_date filter is too coarse — Kalshi's program
        # end_date can extend past individual market closes within the period.
        if _ticker_settled_already(ticker):
            continue

        # Pick best signal: per-market > per-series > prior
        m = market_stats.get(ticker, {})
        s = series_stats.get(series, {})
        if m.get("n", 0) >= MIN_SNAPS_FOR_OBS:
            share = m["share"]
            qual = m["qual"]
            n_snaps = m["n"]
            confidence = "observed"
        elif s.get("n", 0) >= 20:
            share = s["share"]
            qual = s["qual"]
            n_snaps = s["n"]
            confidence = "series"
        else:
            share = DEFAULT_SHARE_PRIOR
            qual = DEFAULT_QUAL_PRIOR
            n_snaps = 0
            confidence = "prior"

        # Implied competitor count (us + N others split the pool equally)
        # share = 1 / (1 + competitors). Solve: competitors = 1/share - 1.
        if share > 0:
            competitor_est = max(0, 1.0 / share - 1)
        else:
            competitor_est = 999

        # Adverse cost per market per day
        # Period for daily=1d, weekly=7d, monthly=30d. Use reward_per_day to back out.
        a = adverse_stats.get(series, {})
        adverse_per_market = a.get("adverse_per_mkt", 0)
        # Settlement is ONCE per market lifecycle, so /day = adverse / period_days.
        # Easiest proxy: amortize across reward_per_day's implied period:
        # if reward_per_day is small (weekly = $14) we get fewer rebate days too
        # so the daily adverse rate is just adverse_per_market / 7 (weekly).
        # Rough but workable.
        period_days = 7  # default to weekly; conservative for daily markets
        adverse_per_day = adverse_per_market / period_days

        gross_per_day = reward * share * qual
        net_per_day = gross_per_day - adverse_per_day

        results.append({
            "market_ticker":     ticker,
            "series_ticker":     series or "?",
            "reward_per_day":    round(reward, 2),
            "share":             round(share, 4),
            "qual_rate":         round(qual, 3),
            "competitor_est":    round(competitor_est, 1),
            "n_snaps":           n_snaps,
            "adverse_per_day":   round(adverse_per_day, 4),
            "gross_per_day":     round(gross_per_day, 4),
            "net_per_day":       round(net_per_day, 4),
            "confidence":        confidence,
            "target_size":       target,
            "discount_factor":   df or 0.5,
            "end_date":          end_date,
        })

    results.sort(key=lambda d: -d["net_per_day"])
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top", type=int, default=30, help="Show top N markets")
    p.add_argument("--series", default=None, help="Filter to series prefix")
    p.add_argument("--quoted", action="store_true",
                   help="Only show markets where we have real Kalshi resting orders")
    p.add_argument("--positive-only", action="store_true",
                   help="Only show markets with positive net_per_day")
    a = p.parse_args()

    quoted_set: set[str] = set()
    if a.quoted:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from execution.kalshi_auth import KalshiClient
            c = KalshiClient()
            r = c.get("/portfolio/orders", params={"status": "resting", "limit": 200})
            for o in r.get("orders", []):
                if o.get("ticker"):
                    quoted_set.add(o["ticker"])
        except Exception as e:
            print(f"WARN: couldn't fetch Kalshi orders ({e}); skipping --quoted filter")
            quoted_set = set()

    results = scan()
    if a.series:
        results = [r for r in results if r["series_ticker"].startswith(a.series)]
    if a.quoted:
        results = [r for r in results if r["market_ticker"] in quoted_set]
    if a.positive_only:
        results = [r for r in results if r["net_per_day"] > 0]

    print(f"\n══ COMPETITOR DENSITY SCAN ══")
    print(f"  showing top {min(a.top, len(results))} of {len(results)} markets")
    if a.quoted:
        print(f"  filter: currently quoted on Kalshi ({len(quoted_set)} resting tickers)")
    print()
    print(f"  {'rank':>4s}  {'ticker':<40s}  {'reward':>7s}  {'share':>6s}  "
          f"{'comp':>4s}  {'qual':>5s}  {'adv/d':>7s}  {'NET/d':>7s}  conf")
    print(f"  {'-'*4}  {'-'*40}  {'-'*7}  {'-'*6}  {'-'*4}  {'-'*5}  {'-'*7}  {'-'*7}  -----")
    for i, r in enumerate(results[:a.top], 1):
        print(f"  {i:>4d}  {r['market_ticker'][:40]:<40s}  "
              f"${r['reward_per_day']:>6.2f}  "
              f"{r['share']:>6.3f}  "
              f"{r['competitor_est']:>4.1f}  "
              f"{r['qual_rate']:>5.2f}  "
              f"${r['adverse_per_day']:>6.3f}  "
              f"${r['net_per_day']:>6.3f}  "
              f"{r['confidence']}")

    # Series rollup
    print(f"\n══ SERIES ROLLUP (top 15) ══")
    by_series = defaultdict(lambda: {"n": 0, "net": 0.0, "gross": 0.0, "reward": 0.0})
    for r in results:
        s = r["series_ticker"]
        by_series[s]["n"] += 1
        by_series[s]["net"] += r["net_per_day"]
        by_series[s]["gross"] += r["gross_per_day"]
        by_series[s]["reward"] += r["reward_per_day"]
    series_rank = sorted(by_series.items(), key=lambda kv: -kv[1]["net"])
    print(f"  {'series':<28s}  {'n':>3s}  {'reward/d':>9s}  {'gross/d':>8s}  {'NET/d':>8s}")
    for s, d in series_rank[:15]:
        print(f"  {s:<28s}  {d['n']:>3d}  ${d['reward']:>7.2f}  "
              f"${d['gross']:>6.2f}  ${d['net']:>6.2f}")

    if a.positive_only or len(results) < 5:
        return 0

    # Bottom 10 (likely losers)
    print(f"\n══ BOTTOM 10 (consider dropping) ══")
    print(f"  {'ticker':<40s}  {'NET/d':>7s}  {'why':<30s}")
    for r in results[-10:][::-1]:
        why = ""
        if r["adverse_per_day"] > r["gross_per_day"]:
            why = "adverse > gross"
        elif r["share"] < 0.05:
            why = f"diluted (~{r['competitor_est']:.0f} competitors)"
        elif r["qual_rate"] < 0.3:
            why = f"qual_rate low ({r['qual_rate']*100:.0f}%)"
        else:
            why = "low headline reward"
        print(f"  {r['market_ticker'][:40]:<40s}  ${r['net_per_day']:>6.3f}  {why}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
