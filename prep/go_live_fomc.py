"""One-button FOMC live flip.

Runs full pre-flight + if all gates green AND user explicitly --approve,
executes the thesis against real Kalshi.

Usage:
    # Dry-run preview (always safe, no orders):
    python prep/go_live_fomc.py --event fomc_20260430 --bankroll 5000 --check

    # Paper-mode run (logs orders as paper, SOV_LIVE not required):
    python prep/go_live_fomc.py --event fomc_20260430 --bankroll 5000 --paper

    # LIVE execution (requires explicit flag + env var SOV_LIVE=true):
    SOV_LIVE=true python prep/go_live_fomc.py \\
        --event fomc_20260430 --bankroll 5000 --apply --approve

Gates (must all pass for --apply to proceed):
  1. Event is in prep window (T-72h to T+0)
  2. Markets queryable from Kalshi
  3. Corpus for event_type has ≥20 transcripts
  4. Thesis generates at least N HIGH or HOMERUN conviction opportunities
  5. Kalshi balance ≥ event cap × 1.2 (with margin)
  6. No active kill switch (SOV_KILL unset)
  7. User --approve flag present (belt-and-suspenders)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_calendar.events import get_event
from prep.corpus_compiler import compile_for_event
from prep.fomc_dry_run import heuristic_context_scorer, make_base_rate_fn
from prep.live_executor import KalshiOrderClient, place_event_batch
from prep.term_expander import filter_markets_for_event, query_live_markets
from prep.thesis_builder import build_event_thesis

_log = logging.getLogger(__name__)


def preflight_checks(event, result: dict, bankroll: float,
                     client: KalshiOrderClient, live_mode: bool) -> list[tuple[str, bool, str]]:
    """Returns [(gate, pass, reason), ...]"""
    gates = []

    # Gate 1: Prep window
    now = datetime.now(timezone.utc)
    in_window = event.is_prep_window(now)
    hours_out = (event.event_datetime_utc - now).total_seconds() / 3600
    gates.append(("prep_window", in_window,
                  f"T-minus {hours_out:.1f}h, window is 72h pre-event"))

    # Gate 2: Markets present
    n_markets = result.get("markets_scanned", 0)
    gates.append(("markets_queryable", n_markets >= 10,
                  f"{n_markets} markets returned (need ≥10)"))

    # Gate 3: Corpus size
    corpus_n = result.get("corpus_n", 0)
    gates.append(("corpus_depth", corpus_n >= 20,
                  f"{corpus_n} prior transcripts (need ≥20)"))

    # Gate 4: Enough high-conviction opportunities
    conv = result.get("conviction_counts", {})
    high_conv = conv.get("HOMERUN", 0) + conv.get("HIGH", 0)
    gates.append(("high_conviction_n", high_conv >= 5,
                  f"{high_conv} HOMERUN/HIGH opportunities (need ≥5)"))

    # Gate 5: Kalshi balance
    if live_mode:
        try:
            balance = client.get_balance_usd()
            needed = event.max_position_pct * bankroll * 1.2
            gates.append(("balance_margin", balance >= needed,
                          f"balance=${balance:.2f}, need ${needed:.2f} (120% of event cap)"))
        except Exception as e:
            gates.append(("balance_margin", False, f"balance fetch failed: {e}"))
    else:
        gates.append(("balance_margin", True, "paper — balance check skipped"))

    # Gate 6: Kill switch
    gates.append(("kill_switch_off",
                  os.environ.get("SOV_KILL", "").lower() not in ("1", "true", "yes"),
                  "SOV_KILL env"))

    # Gate 7: Sanity — at least SOME recommended $ in thesis
    total = result.get("total_recommended", 0)
    gates.append(("thesis_sized", total > 0,
                  f"${total:.2f} total recommended (need > 0)"))

    return gates


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True, help="event_id (e.g., fomc_20260430)")
    p.add_argument("--bankroll", type=float, required=True, help="bankroll USD for sizing")
    p.add_argument("--check", action="store_true",
                   help="preview only, no orders (default)")
    p.add_argument("--paper", action="store_true",
                   help="paper-mode execution — logs to DB but doesn't hit Kalshi")
    p.add_argument("--apply", action="store_true",
                   help="execute against Kalshi (requires SOV_LIVE=true AND --approve)")
    p.add_argument("--approve", action="store_true",
                   help="explicit human approval (required for --apply)")
    p.add_argument("--output-json", default=None)
    p.add_argument("--use-claude", action="store_true",
                   help="use Claude LLM context scorer (default: heuristic)")
    p.add_argument("--force-window", action="store_true",
                   help="bypass prep_window gate (testing/rehearsal only)")
    a = p.parse_args()

    event = get_event(a.event)
    if event is None:
        _log.error(f"no such event: {a.event}")
        sys.exit(1)

    # Audit #13: --force-window is for rehearsal ONLY. Combining it with --apply
    # could send real orders outside the 72h prep window if --approve is also set.
    # Defense-in-depth: disallow that combination entirely.
    if a.force_window and a.apply:
        _log.error("--force-window incompatible with --apply. "
                   "Rehearsal mode cannot send live orders.")
        sys.exit(1)

    live_mode = os.environ.get("SOV_LIVE", "").lower() in ("1", "true", "yes")

    print(f"\n══════════════════════════════════════════════════════")
    print(f"  GO LIVE — {event.event_id}")
    print(f"══════════════════════════════════════════════════════")
    print(f"  Event:    {event.title}")
    print(f"  When:     {event.event_datetime_utc.isoformat()}")
    print(f"  Bankroll: ${a.bankroll:,.2f}")
    print(f"  SOV_LIVE: {live_mode}")
    print(f"  Mode:     {'LIVE' if a.apply and live_mode else 'PAPER' if a.paper else 'CHECK'}")

    # 1. Pull live markets + corpus + thesis
    print("\n[1/4] Querying Kalshi...")
    raw = query_live_markets(event.event_id, event.kalshi_series[0])
    markets = filter_markets_for_event(raw, event.event_datetime_utc)
    print(f"      → {len(markets)} markets in window")

    print("\n[2/4] Compiling corpus...")
    corpus = compile_for_event(
        event_id=event.event_id, event_type=event.corpus_event_type,
        event_date_iso=event.event_datetime_utc.strftime("%Y-%m-%d"),
        db_path="/root/sovereign/data/sovereign.db",
    )
    print(f"      → corpus_n={corpus.corpus_count}")

    print("\n[3/4] Building thesis...")
    if a.use_claude:
        from prep.context_scorer_claude import make_scorer_fn
        # Enrich with live news for Claude context
        news_items = []
        try:
            from prep.news_aggregator import fetch_fed_news, as_context_lines
            raw = fetch_fed_news(max_age_hours=72, max_headlines=25)
            news_items = as_context_lines(raw, max_n=25)
            print(f"      → pulled {len(raw)} news headlines")
        except Exception as e:
            print(f"      → news fetch failed: {e} (continuing without)")
        # Also pass intermeeting Fed speeches (powerful predictor)
        speeches = corpus.intermeeting_speeches or []
        print(f"      → intermeeting speeches: {len(speeches)}")
        scorer = make_scorer_fn(
            news_headlines=news_items,
            intermeeting_speeches=speeches,
        )
    else:
        scorer = heuristic_context_scorer

    result = build_event_thesis(
        markets, corpus,
        base_rate_fn=make_base_rate_fn("/root/sovereign/data/sovereign.db"),
        context_scorer_fn=scorer,
        bankroll_usd=a.bankroll,
        max_event_pct=event.max_position_pct,
    )

    # 2. Pre-flight
    print(f"\n[4/4] Pre-flight gates:")
    client = KalshiOrderClient()
    gates = preflight_checks(event, result, a.bankroll, client,
                              live_mode=(a.apply and live_mode))
    if a.force_window:
        gates = [(n, True if n == "prep_window" else p, r) for n, p, r in gates]
    all_pass = all(p for _, p, _ in gates)
    for name, passed, reason in gates:
        mark = "✅" if passed else "❌"
        forced = " (FORCED)" if name == "prep_window" and a.force_window else ""
        print(f"  {mark} {name:<22s}  {reason}{forced}")

    print(f"\n  conviction counts: {result['conviction_counts']}")
    print(f"  total recommended: ${result['total_recommended']:,.2f}")

    # 3. Execute if applicable
    if a.check:
        print("\n  --check only, exiting.")
        if a.output_json:
            check_out = {
                "event_id": event.event_id,
                "mode": "check",
                "thesis_summary": {
                    "markets_scanned": result["markets_scanned"],
                    "opportunities":   result["opportunities"],
                    "total_recommended": result["total_recommended"],
                    "conviction_counts": result["conviction_counts"],
                },
                "theses": result["theses"],
            }
            with open(a.output_json, "w") as f:
                json.dump(check_out, f, indent=2)
            print(f"  Output: {a.output_json}")
        return

    if not all_pass:
        print("\n  ❌ GATES FAILED — refusing to execute.")
        sys.exit(2)

    if a.apply:
        if not a.approve:
            print("\n  ⚠ --apply requires --approve. Aborting.")
            sys.exit(3)
        if not live_mode:
            print("\n  ⚠ --apply requires SOV_LIVE=true env. Aborting.")
            sys.exit(4)

    dry_run = a.paper and not a.apply
    print(f"\n  ⚡ EXECUTING BATCH (dry_run={dry_run}, live={a.apply and live_mode})")
    placed = place_event_batch(
        result["theses"], event_id=event.event_id, bankroll=a.bankroll,
        client=client, dry_run=dry_run,
    )

    print(f"\n  Placed: {len(placed)} orders")
    acc = sum(1 for p in placed if p.status == "accepted")
    paper = sum(1 for p in placed if p.status == "paper")
    rej = sum(1 for p in placed if p.status == "rejected")
    err = sum(1 for p in placed if p.status == "error")
    print(f"  accepted={acc} paper={paper} rejected={rej} errored={err}")

    if rej or err:
        print("\n  Rejections/errors:")
        for p in placed:
            if p.status in ("rejected", "error"):
                print(f"    {p.ticker}  {p.status}  {p.reason}")

    if a.output_json:
        out = {
            "event_id": event.event_id,
            "mode": "live" if (a.apply and live_mode) else "paper",
            "placed": [
                {"ticker": p.ticker, "side": p.side, "price": p.price_cents,
                 "contracts": p.contracts, "order_id": p.order_id,
                 "status": p.status, "reason": p.reason}
                for p in placed
            ],
            "thesis_summary": {
                "markets_scanned": result["markets_scanned"],
                "opportunities":   result["opportunities"],
                "total_recommended": result["total_recommended"],
                "conviction_counts": result["conviction_counts"],
            },
        }
        with open(a.output_json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\n  Output: {a.output_json}")


if __name__ == "__main__":
    main()
