"""FOMC Sprint 1 dry run — full end-to-end against live Kalshi data.

Usage:
    PYTHONPATH=. python prep/fomc_dry_run.py [--event-id fomc_20260430]

What it does:
  1. Load event from calendar
  2. Query live Kalshi for KXFEDMENTION markets on that date
  3. Compile corpus from Sovereign DB
  4. For each market: base_rate_fn (real SQL) + context_scorer (heuristic)
  5. Build event thesis
  6. Print ranked opportunity report

No orders are placed. Output is human-reviewable plan.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_calendar.events import get_event, next_event
from prep.corpus_compiler import compile_for_event
from prep.term_expander import filter_markets_for_event, query_live_markets
from prep.thesis_builder import build_event_thesis

_log = logging.getLogger(__name__)

SOVEREIGN_DB = "/root/sovereign/data/sovereign.db"


def make_base_rate_fn(db_path: str = SOVEREIGN_DB):
    """Returns a function that queries Sovereign's frequency_matrix.

    Tries each variant of the word; returns the best (highest k/n) match.
    Falls back to corpus-wide regex search if frequency_matrix miss.
    """
    def _lookup(event_type: str, variants: list[str]) -> tuple[float, int, int]:
        conn = sqlite3.connect(db_path)
        try:
            best = (0.0, 0, 0)
            for v in variants:
                row = conn.execute(
                    """SELECT occurrences, total_events, base_rate
                       FROM frequency_matrix
                       WHERE event_type = ? AND LOWER(term) = ?""",
                    (event_type, v.lower()),
                ).fetchone()
                if row is None:
                    continue
                k, n, br = row
                if n > best[2]:
                    best = (br, k, n)

            if best[2] > 0:
                return best

            # Fallback: count in raw transcripts
            rows = conn.execute(
                "SELECT raw_text FROM transcripts WHERE event_type = ?",
                (event_type,),
            ).fetchall()
            if not rows:
                return (0.0, 0, 0)
            n = len(rows)
            k = 0
            for (text,) in rows:
                if not text:
                    continue
                text_lower = text.lower()
                if any(re.search(rf"\b{re.escape(v)}\b", text_lower) for v in variants):
                    k += 1
            return (k / n if n else 0.0, k, n)
        finally:
            conn.close()

    return _lookup


def heuristic_context_scorer(
    word: str, base_rate: float, recent_transcripts: list[dict],
) -> tuple[float, str]:
    """Heuristic context adjustment — no LLM call needed.

    Logic:
      - If word mentioned in BOTH recent transcripts → +0.4 logit (momentum)
      - If word mentioned in ONLY last transcript → +0.2 (emerging)
      - If word NOT in recent but base rate >0.80 → 0.0 (stable)
      - If word NOT in recent and base rate <0.10 → -0.1 (confirm rare)
    """
    if not recent_transcripts:
        return (0.0, "no recent context")

    word_lower = word.lower()
    hits = [1 if re.search(rf"\b{re.escape(word_lower)}\b",
                            (t.get("raw_text", "") or "").lower())
            else 0
            for t in recent_transcripts]
    total = sum(hits)

    if total == len(recent_transcripts) and total >= 2:
        return (0.4, f"mentioned in all {total} recent events — momentum")
    if total == 1:
        return (0.2, f"mentioned in most-recent event — emerging")
    if base_rate > 0.80:
        return (0.0, f"not recent but base_rate={base_rate:.0%} stable")
    if base_rate < 0.10:
        return (-0.1, f"absent from recent + rare historically — confirm low")
    return (0.0, "neutral — not in recent, middle base rate")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--event-id", default=None,
                   help="default: next upcoming FOMC event")
    p.add_argument("--bankroll", type=float, default=5000.0,
                   help="assumed bankroll USD for sizing")
    p.add_argument("--db", default=SOVEREIGN_DB)
    p.add_argument("--output-json", default=None,
                   help="write full thesis to JSON file")
    a = p.parse_args()

    event = get_event(a.event_id) if a.event_id else next_event()
    if event is None:
        _log.error("no event found")
        return

    print(f"\n══════════════════════════════════════════════════════")
    print(f"  FOMC HOMERUN DRY RUN — {event.event_id}")
    print(f"══════════════════════════════════════════════════════")
    print(f"  Title:        {event.title}")
    print(f"  Event time:   {event.event_datetime_utc.isoformat()}")
    now = datetime.now(timezone.utc)
    hours_out = (event.event_datetime_utc - now).total_seconds() / 3600
    print(f"  Now:          {now.isoformat()}")
    print(f"  T-minus:      {hours_out:.1f} hours")
    print(f"  Prep window:  {'YES' if event.is_prep_window(now) else 'NO'}")
    print(f"  Bankroll:     ${a.bankroll:,.2f}")

    # 1. Live market query
    print(f"\n[1/4] Querying live Kalshi {event.kalshi_series[0]}...")
    raw = query_live_markets(event.event_id, event.kalshi_series[0])
    print(f"      → {len(raw)} raw markets returned by API")

    markets = filter_markets_for_event(raw, event.event_datetime_utc, tolerance_hours=24)
    print(f"      → {len(markets)} mention markets match this event window")

    if not markets:
        _log.error("no markets found — aborting")
        return

    # 2. Corpus compile
    print(f"\n[2/4] Compiling corpus for {event.corpus_event_type}...")
    event_date_iso = event.event_datetime_utc.strftime("%Y-%m-%d")
    corpus = compile_for_event(
        event_id=event.event_id,
        event_type=event.corpus_event_type,
        event_date_iso=event_date_iso,
        db_path=a.db,
    )
    print(f"      → corpus: {corpus.summary()}")

    # 3. Build thesis
    print(f"\n[3/4] Scoring {len(markets)} markets...")
    base_rate_fn = make_base_rate_fn(a.db)

    result = build_event_thesis(
        markets, corpus,
        base_rate_fn=base_rate_fn,
        context_scorer_fn=heuristic_context_scorer,
        bankroll_usd=a.bankroll,
        max_event_pct=event.max_position_pct,
    )

    # 4. Report
    print(f"\n[4/4] Thesis report")
    print(f"      markets_scanned:   {result['markets_scanned']}")
    print(f"      opportunities:     {result['opportunities']}")
    print(f"      skipped:           {result['skipped']}")
    print(f"      conviction counts: {result['conviction_counts']}")
    print(f"      total_recommended: ${result['total_recommended']:,.2f}")
    print(f"      scaling:           {result['scaling']}")

    if result["theses"]:
        print(f"\n=== Ranked opportunities ===")
        print(f"{'tkr':<30s} {'word':<24s} {'conv':<8s} {'side':<4s} "
              f"{'edge':>7s} {'size$':>7s} {'reason':<50s}")
        for t in result["theses"][:30]:
            tkr_short = t['ticker'].replace("KXFEDMENTION-", "")
            print(f"  {tkr_short:<28s} {t['word'][:22]:<24s} {t['conviction']:<8s} "
                  f"{t['best_side']:<4s} {t['best_edge']*100:>+5.1f}%  "
                  f"${t['recommended_usd']:>6.2f}  {t['reasoning'][:50]}")
    else:
        print("\n  NO opportunities identified (all markets illiquid + no edge from base rate alone)")
        # Show what MIGHT happen when prices appear — fake a 50¢ mid on the top 10
        # most confident mispricings based on base rate alone
        print("\n  Illiquid preview (if markets opened at 50¢):")
        from prep.thesis_builder import build_thesis
        from prep.term_expander import MentionMarket
        preview = []
        for m in markets:
            fake = MentionMarket(
                ticker=m.ticker, title=m.title, word_raw=m.word_raw,
                word_variants=m.word_variants, close_time_utc=m.close_time_utc,
                yes_bid=48, yes_ask=52,
            )
            t = build_thesis(fake, corpus, base_rate_fn,
                             heuristic_context_scorer, a.bankroll)
            preview.append(t)
        preview.sort(key=lambda t: -abs(t.best_edge))
        print(f"  {'word':<30s} {'base_rate':>9s} {'edge@50¢':>9s} {'side':<4s} {'conv':<8s}")
        for t in preview[:15]:
            print(f"    {t.word_raw[:28]:<30s} {t.base_rate*100:>7.1f}% "
                  f"{t.best_edge*100:>+7.1f}%  {t.best_side:<4s} {t.conviction}")

    if a.output_json:
        with open(a.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\n  Full thesis written to: {a.output_json}")


if __name__ == "__main__":
    main()
