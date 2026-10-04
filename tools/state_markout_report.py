#!/usr/bin/env python3
"""5-minute markout by fill-price bucket from the engine state file (read-only).

    python -m tools.state_markout_report [--state /var/lib/lip-maker/engine_state.json] [--json]

Uses the persisted event-level accumulator (``price_event_acc``): each EVENT is one
observation (fills inside one event are correlated), one-sided 90% Student-t
interval via mm/unattended/go_no_go.py. Prints a recommendation per bucket but
NEVER applies anything: a price floor is the opt-in knob LIP_MIN_SIDE_PRICE_CENTS
and stays an operator decision. Sub-10c / 10-30c are the buckets where published
Kalshi studies find the largest losses; this is OUR data for that question.
"""
from __future__ import annotations

import argparse
import json
import sys

from mm.unattended import go_no_go

BUCKETS = ("<10", "10-30", "30-70", "70-90", ">=90")
FLOOR_FOR = {"<10": 10, "10-30": 30}     # LIP_MIN_SIDE_PRICE_CENTS value that would drop the bucket
MIN_EVENTS = 30
NOTE = ("advisory: recommendations are never applied; fills inside one event count once; "
        "paper fills only; an interval spanning 0 means keep collecting")


def build_report(path: str) -> dict:
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        data = {}
    raw = data.get("price_event_acc") if isinstance(data, dict) else None
    rows = []
    for b in BUCKETS:
        events = (raw or {}).get(b) or {}
        try:
            stats = go_no_go.event_stats({e: [int(r[0]), float(r[1]), float(r[2])] for e, r in events.items()})
        except (IndexError, TypeError, ValueError):
            stats = go_no_go.event_stats({})
        rec = "insufficient data"
        upper = None
        if stats["events"] >= MIN_EVENTS and stats["se_cents"] is not None:
            upper = stats["mean_cents"] + go_no_go.t_crit_90(stats["events"] - 1) * stats["se_cents"]
            if upper < 0:
                rec = (f"consider a floor (LIP_MIN_SIDE_PRICE_CENTS={FLOOR_FOR[b]})" if b in FLOOR_FOR
                       else "adverse: review manually (no price-floor knob applies)")
            else:
                rec = "no action"
        rows.append({"bucket": b, "events": stats["events"], "fills": stats["fills"],
                     "mean_cents": stats["mean_cents"], "lower_90_cents": stats["lower_90_cents"],
                     "upper_90_cents": upper, "recommendation": rec})
    series = []
    for name, a in sorted((data.get("series_acc") or {}).items()) if isinstance(data, dict) else []:
        try:
            c = float(a.get("mk5_contracts") or 0.0)
            series.append({"series": name, "fills": int(a.get("mk5_n") or 0),
                           "mean_cents": None if c <= 0 else float(a["mk5_usd"]) * 100.0 / c,
                           "note": "no interval: fills are not independent"})
        except (TypeError, ValueError, KeyError):
            continue
    return {"state": path, "buckets": rows, "series": series, "note": NOTE}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", default="/var/lib/lip-maker/engine_state.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = build_report(args.state)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    fmt = lambda v: "-" if v is None else f"{v:+.2f}"
    print(f"{'bucket':>7} {'events':>6} {'fills':>6} {'mean c':>7} {'lo90':>7} {'hi90':>7}  recommendation")
    for r in rep["buckets"]:
        print(f"{r['bucket']:>7} {r['events']:>6} {r['fills']:>6} {fmt(r['mean_cents']):>7} "
              f"{fmt(r['lower_90_cents']):>7} {fmt(r['upper_90_cents']):>7}  {r['recommendation']}")
    print("\nper series (mean only):")
    for r in rep["series"]:
        print(f"  {r['series']:<14} fills {r['fills']:>4}  mean {fmt(r['mean_cents'])}c")
    print("\n" + rep["note"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
