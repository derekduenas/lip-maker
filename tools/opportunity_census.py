#!/usr/bin/env python3
"""How many CURRENT programs survive each filter, and why the rest fail.

    python tools/opportunity_census.py --book-sample 60

Staged on purpose. Metadata filters are free and run over the whole live
universe; only the survivors get a book fetched, and that is capped so the
scan stays inside rate limits. History is never re-downloaded: the scan
asks for `active` (and optionally `upcoming`) only.

The funnel is reported as UNIQUE PROGRAM IDS at every stage, because a
single ticker can carry more than one program and a row count is not an
opportunity count.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import constitution, settings
from engine.lip_discovery import discover_result
from engine.market_clock import MarketClock
from execution.rest_book_feed import RestBookFeed, _cents
from run_paper import _program_params_from_market

SIZE_FLOOR = 10


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-sample", type=int, default=60,
                    help="max markets to fetch books for (rate-limit budget)")
    ap.add_argument("--include-upcoming", action="store_true")
    ap.add_argument("--out", default="data/census")
    a = ap.parse_args()

    bank = float(getattr(settings, "BANKROLL_USD", 80))
    cap = bank * constitution.MAX_PER_MARKET_PCT

    funnel = OrderedDict()
    reasons = {}

    statuses = ["active"] + (["upcoming"] if a.include_upcoming else [])
    rows = []
    counts = {}
    for st in statuses:
        r = discover_result(status=st, save=False)
        rows.extend(r.programs)
        counts[st] = r.counts()
    funnel["rows_returned"] = sum(c["rows_returned"] for c in counts.values())
    funnel["unique_program_ids"] = len({p.get("id") for p in rows if p.get("id")})

    now = time.time()
    started = []
    for p in rows:
        try:
            pp = _program_params_from_market(p)
        except Exception:
            continue
        if not pp.start_ts or not pp.end_ts:
            continue
        if pp.end_ts <= now:
            continue
        if pp.start_ts > now:
            continue
        if pp.period_reward_usd <= 0 or pp.target_size <= 0:
            continue
        started.append((pp, p))
    funnel["program_running_now"] = len({pp.program_id for pp, _ in started})

    by_ticker = {}
    for pp, p in started:
        by_ticker.setdefault(pp.market_ticker, []).append((pp, p))
    funnel["unique_markets"] = len(by_ticker)

    # Rank by pool rate before spending the book budget.
    ranked = sorted(by_ticker.items(),
                    key=lambda kv: -max(x[0].pool_rate_usd_per_sec for x in kv[1]))
    budget = ranked[:a.book_sample]
    funnel["book_fetch_budget"] = len(budget)

    clock = MarketClock()
    feed = RestBookFeed()
    open_now = two_sided = feasible = 0
    candidates = []
    for tkr, progs in budget:
        ct = clock.close_time(tkr)
        if ct.open_ts is None or not (ct.open_ts <= now < (ct.close_ts or 0)):
            reasons["market_not_open"] = reasons.get("market_not_open", 0) + 1
            continue
        open_now += 1
        try:
            payload = feed._fetch_sync(tkr)
        except Exception:
            reasons["book_fetch_failed"] = reasons.get("book_fetch_failed", 0) + 1
            continue
        inner = (payload or {}).get("orderbook_fp") or {}
        y, n = inner.get("yes_dollars") or [], inner.get("no_dollars") or []
        if not (y and n):
            reasons["one_sided_book"] = reasons.get("one_sided_book", 0) + 1
            continue
        two_sided += 1
        yb = max(_cents(r[0]) for r in y)
        nb = max(_cents(r[0]) for r in n)
        unit = (yb + nb) / 100.0
        max_size = int(cap / unit) if unit > 0 else 0
        pp = max(progs, key=lambda x: x[0].pool_rate_usd_per_sec)[0]
        rec = {"ticker": tkr, "program_id": pp.program_id,
               "pool_rate_usd_per_sec": round(pp.pool_rate_usd_per_sec, 8),
               "target_size": pp.target_size,
               "discount_factor": pp.discount_factor,
               "yes_bid": yb, "no_bid": nb, "yes_plus_no": yb + nb,
               "duration_min": round(ct.duration_min or 0, 1),
               "per_market_cap_usd": round(cap, 2),
               "largest_legal_size": max_size,
               "sentinel_feasible": max_size >= SIZE_FLOOR,
               "bankroll_needed_usd": round(
                   SIZE_FLOOR * unit / constitution.MAX_PER_MARKET_PCT, 2)}
        if rec["sentinel_feasible"]:
            feasible += 1
        else:
            reasons["sentinel_infeasible"] = reasons.get("sentinel_infeasible", 0) + 1
        candidates.append(rec)

    funnel["market_open_now"] = open_now
    funnel["two_sided_book"] = two_sided
    funnel["sentinel_feasible"] = feasible

    candidates.sort(key=lambda r: (-r["sentinel_feasible"],
                                   -r["pool_rate_usd_per_sec"]))
    out = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "sentinel_bankroll_usd": bank,
           "account_ledger_usd": float(getattr(settings,
                                               "ACCOUNT_OPENING_CASH_USD", 5000.0)),
           "per_market_cap_usd": round(cap, 2), "size_floor": SIZE_FLOOR,
           "discovery_counts": counts, "funnel": funnel,
           "rejection_reasons": reasons,
           "candidates": candidates[:40]}
    d = Path(a.out); d.mkdir(parents=True, exist_ok=True)
    p = d / f"census-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    p.write_text(json.dumps(out, indent=2) + "\n")

    print("=" * 70)
    print("OPPORTUNITY CENSUS (unique program ids unless stated)")
    print("=" * 70)
    for k, v in funnel.items():
        print(f"  {k:26} {v}")
    print("\n  rejections:", reasons or "none")
    print(f"\n  sentinel bankroll ${bank:,.2f} -> per-market cap ${cap:,.2f}, "
          f"size floor {SIZE_FLOOR}")
    print("\n  strongest candidates (by pool rate):")
    hdr = (f"  {'ticker':42} {'$/sec':>9} {'tgt':>6} {'y+n':>4} "
           f"{'maxsz':>6} {'ok':>3} {'need$':>8}")
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for r in candidates[:12]:
        print(f"  {r['ticker']:42} {r['pool_rate_usd_per_sec']:9.6f} "
              f"{r['target_size']:6.0f} {r['yes_plus_no']:4d} "
              f"{r['largest_legal_size']:6d} "
              f"{'yes' if r['sentinel_feasible'] else 'no':>3} "
              f"{r['bankroll_needed_usd']:8.2f}")
    print(f"\n  report: {p}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
