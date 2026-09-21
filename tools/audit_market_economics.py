#!/usr/bin/env python3
"""Worked LIP calculation for one market, against the published rules.

    python tools/audit_market_economics.py --stream <stream.jsonl> [--ticker T]

Why this exists
---------------
engine/quote_economics.py obtains its reward share from
cross_venue.yield_equation.MarketYield, whose `our_share` is

    our_size / (top_book_size + our_size)

a raw depth ratio. That is NOT the program's rule. Kalshi scores
QUALIFYING, DISTANCE-WEIGHTED depth:

  * a side qualifies only if cumulative bid depth reaches TargetSize; the
    price at which it does is the CUTOFF;
  * the REFERENCE is the level at which cumulative depth reaches
    TargetSize/5;
  * every level at price >= cutoff scores DiscountFactor^(reference-price)
    x size. Levels below the cutoff score NOTHING and must not appear in
    the denominator;
  * each side normalises to 1.0, so a snapshot pays out 2.0 in total and
    our fraction is our_total_score / 2.

Two consequences the ratio form gets wrong:

  1. Deep books are not automatically diluting. Depth outside the cutoff is
     excluded, and depth far from the reference is discounted geometrically.
  2. We do not have to supply TargetSize ourselves. Existing liquidity
     counts toward qualification; our order only has to be inside the
     cutoff to earn a share of an already-qualifying side.

This tool prints the full arithmetic so the two can be compared on real
captured books.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.lip_scorer import (
    OurQuotes, ProgramParams, _find_cutoff_price, _score_bids, score_snapshot,
    snapshot_share)
from execution.kalshi_ws import BookLevel, BookState


def _book_from_event(ev) -> BookState:
    b = BookState(market_ticker=ev["ticker"])
    b.yes_bids = sorted([BookLevel(int(p), float(s)) for p, s in ev["yes"]],
                        key=lambda l: -l.price_cents)
    b.no_bids = sorted([BookLevel(int(p), float(s)) for p, s in ev["no"]],
                       key=lambda l: -l.price_cents)
    b.snapshot_count = 1
    return b


def side_report(name, bids, target, df, our_price, our_size):
    cutoff = _find_cutoff_price(bids, target)
    ref = _find_cutoff_price(bids, target / 5.0)
    total_depth = sum(l.size for l in bids)
    out = {
        "side": name,
        "levels": len(bids),
        "best_price": bids[0].price_cents if bids else None,
        "total_book_depth": round(total_depth, 2),
        "target_size": target,
        "qualifies": cutoff is not None,
        "cutoff_price": cutoff,
        "reference_price": ref,
    }
    if cutoff is None:
        out["note"] = ("side does NOT reach TargetSize, so it cannot "
                       "qualify and nothing scores")
        return out, 0.0, 0.0
    qual_levels = [l for l in bids if l.price_cents >= cutoff]
    out["levels_inside_cutoff"] = len(qual_levels)
    out["depth_inside_cutoff"] = round(sum(l.size for l in qual_levels), 2)
    out["depth_excluded_below_cutoff"] = round(
        total_depth - sum(l.size for l in qual_levels), 2)
    ours = [BookLevel(int(our_price), float(our_size))] if our_size > 0 else []
    our_score, total_score = _score_bids(bids, ours, ref, df, cutoff)
    out["weighted_qualifying_depth"] = round(total_score, 4)
    out["weighting_note"] = (
        f"each level scores {df}^(reference {ref} - price) x size; "
        "levels below the cutoff score nothing")
    out["our_price"] = our_price
    out["our_size"] = our_size
    out["our_distance_ticks"] = max(0, (ref or 0) - int(our_price))
    out["our_weight"] = round(df ** out["our_distance_ticks"], 6)
    out["our_weighted_score"] = round(our_score, 6)
    out["our_normalised_share_this_side"] = (
        round(our_score / total_score, 8) if total_score > 0 else 0.0)
    out["our_order_inside_cutoff"] = int(our_price) >= cutoff
    return out, our_score, total_score


def audit(book: BookState, params: ProgramParams, our_yes, our_no,
          our_size) -> dict:
    df = params.discount_factor
    target = params.target_size
    y, ys, yt = side_report("yes", book.yes_bids, target, df, our_yes, our_size)
    n, ns, nt = side_report("no", book.no_bids, target, df, our_no, our_size)

    ours = OurQuotes(
        yes_bids=[BookLevel(int(our_yes), float(our_size))] if our_size else [],
        no_bids=[BookLevel(int(our_no), float(our_size))] if our_size else [])
    sc = score_snapshot(book, ours, params)
    share = snapshot_share(sc)

    horizon = params.period_seconds
    payout = share * params.pool_rate_usd_per_sec * horizon

    # The ratio form the economics layer actually used.
    top_both = ((book.yes_bids[0].size if book.yes_bids else 0.0)
                + (book.no_bids[0].size if book.no_bids else 0.0))
    ratio_share = our_size / max(1.0, top_both + our_size)

    return {
        "market": book.market_ticker,
        "program": {
            "program_id": params.program_id,
            "target_size": params.target_size,
            "discount_factor": params.discount_factor,
            "period_reward_usd": params.period_reward_usd,
            "period_seconds": params.period_seconds,
            "pool_rate_usd_per_sec": round(params.pool_rate_usd_per_sec, 10),
        },
        "yes": y, "no": n,
        "snapshot": {
            "valid_two_sided": sc.snapshot_valid,
            "our_total_score": round(sc.our_total_score, 8),
            "our_snapshot_share": round(share, 10),
            "share_note": ("each side normalises to 1.0, so a valid snapshot "
                           "pays 2.0 in total and our fraction is "
                           "our_total_score / 2"),
        },
        "payout": {
            "horizon_sec": horizon,
            "estimated_usd_over_full_period": round(payout, 8),
        },
        "comparison": {
            "rules_based_share": round(share, 10),
            "ratio_form_share_used_by_economics": round(ratio_share, 10),
            "ratio_denominator": round(top_both + our_size, 2),
            "ratio_form_note": (
                "our_size / (best-yes-depth + best-no-depth + our_size). It "
                "sums depth ACROSS BOTH SIDES into a single one-sided "
                "denominator, ignores the cutoff, and applies no distance "
                "weighting."),
            "ratio_over_rules": (round(ratio_share / share, 4)
                                 if share > 0 else None),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", required=True)
    ap.add_argument("--ticker", default="")
    ap.add_argument("--size", type=float, default=1.0)
    ap.add_argument("--target", type=float, default=1000.0)
    ap.add_argument("--discount", type=float, default=0.5)
    ap.add_argument("--pool", type=float, default=200.0)
    ap.add_argument("--period", type=float, default=630240 * 60.0)
    ap.add_argument("--max", type=int, default=3)
    a = ap.parse_args()

    events = [json.loads(l) for l in Path(a.stream).read_text().splitlines() if l]
    latest: dict = {}
    for e in events:
        if e["kind"] == "book":
            latest[e["ticker"]] = e
    tickers = [a.ticker] if a.ticker else list(latest)[:a.max]
    for t in tickers:
        ev = latest.get(t)
        if ev is None:
            print(f"{t}: not in stream"); continue
        book = _book_from_event(ev)
        if not book.yes_bids or not book.no_bids:
            print(f"{t}: one-sided book"); continue
        params = ProgramParams(market_ticker=t, program_id="(cli)",
                               target_size=a.target, discount_factor=a.discount,
                               period_reward_usd=a.pool, period_seconds=a.period)
        our_yes = book.yes_bids[0].price_cents
        our_no = book.no_bids[0].price_cents
        print(json.dumps(audit(book, params, our_yes, our_no, a.size), indent=2))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
