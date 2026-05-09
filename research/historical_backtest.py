"""Historical backtest — validate thesis_builder's probability predictions
against known outcomes across 74 FOMC pressers in the corpus.

Two modes:
  --mode corpus    : scale test — replay base-rate predictions for each
                     mention-market word across every past presser. Validates
                     that our 70% predictions actually resolve YES 70% of
                     the time.
  --mode kalshi    : ground-truth — replay thesis_builder against the 54 real
                     settled KXFEDMENTION markets from March 18, 2026.
                     Shows what our live P&L would have been.

Output: calibration report + (for kalshi mode) simulated trade P&L.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.corpus_compiler import compile_for_event
from prep.thesis_builder import build_thesis
from prep.term_expander import MentionMarket
from prep.fomc_dry_run import heuristic_context_scorer, make_base_rate_fn

_log = logging.getLogger(__name__)

SOVEREIGN_DB = "/root/sovereign/data/sovereign.db"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ─── Canonical mention-market word list ────────────────────────────────
# Derived from actual KXFEDMENTION markets listed by Kalshi for April 2026.
# Each is a (ticker_suffix, raw_word, list_of_variants) triple.
CANONICAL_TERMS = [
    ("AI",    "AI / Artificial Intelligence", ["ai", "artificial intelligence"]),
    ("ANCH",  "Anchor / Anchored",            ["anchor", "anchored"]),
    ("BALA",  "Balance Sheet",                ["balance sheet"]),
    ("BITC",  "Bitcoin",                      ["bitcoin"]),
    ("CENT",  "Central Bank",                 ["central bank"]),
    ("CONS",  "Consumer Confidence",          ["consumer confidence"]),
    ("CRED",  "Credit",                       ["credit"]),
    ("DISS",  "Dissent",                      ["dissent"]),
    ("DOLL",  "Dollar",                       ["dollar"]),
    ("DOT",   "Dot Plot",                     ["dot plot"]),
    ("EGG",   "Egg",                          ["egg"]),
    ("EXPE",  "Expectation",                  ["expectation"]),
    ("GAS",   "Gas / Gasoline / Natural Gas", ["gas", "gasoline", "natural gas"]),
    ("GOLD",  "Gold",                         ["gold"]),
    ("GOOD",  "Goods inflation",              ["goods inflation"]),
    ("GOODA", "Good Afternoon",               ["good afternoon"]),
    ("IRAN",  "Iran",                         ["iran"]),
    ("KALS",  "Kalshi",                       ["kalshi"]),
    ("LAYO",  "Layoff",                       ["layoff"]),
    ("MEDI",  "Median",                       ["median"]),
    ("NATI",  "National Debt",                ["national debt"]),
    ("OIL",   "Oil",                          ["oil"]),
    ("PAND",  "Pandemic",                     ["pandemic"]),
    ("PARD",  "Pardon",                       ["pardon"]),
    ("PRES",  "President",                    ["president"]),
    ("PROB",  "Probability",                  ["probability"]),
    ("PROD",  "Productivity",                 ["productivity"]),
    ("PROJ",  "Projection",                   ["projection"]),
    ("QE",    "QE / Quantitative Easing",     ["qe", "quantitative easing"]),
    ("QT",    "QT / Quantitative Tightening", ["qt", "quantitative tightening"]),
    ("RECE",  "Recession",                    ["recession"]),
    ("RENO",  "Renovation",                   ["renovation"]),
    ("REPL",  "Replace",                      ["replace", "replaces", "replaced", "replacement"]),
    ("REST",  "Restrictive",                  ["restrictive"]),
    ("SHOC",  "Shock",                        ["shock"]),
    ("SHUT",  "Shutdown",                     ["shutdown", "shut down"]),
    ("SLOW",  "Slowdown",                     ["slowdown", "slow down"]),
    ("SOFT",  "Softening",                    ["softening"]),
    ("SOFTL", "Soft Landing",                 ["soft landing"]),
    ("STAG",  "Stagflation",                  ["stagflation"]),
    ("TARI",  "Tariff Inflation",             ["tariff inflation"]),
    ("TAX",   "Tax",                          ["tax"]),
    ("TRAD",  "Trade War",                    ["trade war"]),
    ("TRUM",  "Trump",                        ["trump"]),
    ("UNCE",  "Uncertainty",                  ["uncertainty"]),
    ("UNCH",  "Unchanged",                    ["unchanged"]),
    ("VOLA",  "Volatility",                   ["volatility"]),
    ("YIEL",  "Yield Curve",                  ["yield curve"]),
]


def load_all_pressers(db_path: str = SOVEREIGN_DB) -> list[dict]:
    """Return all FOMC presser transcripts sorted by date."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, event_date, raw_text
               FROM transcripts
               WHERE event_type = 'fomc_presser'
               ORDER BY event_date"""
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "event_date": r[1], "raw_text": r[2] or ""} for r in rows]


def word_in_transcript(text: str, variants: list[str]) -> bool:
    """Does any variant appear in text (whole-word, case-insensitive)?"""
    if not text:
        return False
    tl = text.lower()
    return any(re.search(rf"\b{re.escape(v.lower())}\b", tl) for v in variants)


# ─── Mode 1: corpus-only calibration ───────────────────────────────────
def run_corpus_backtest(db_path: str = SOVEREIGN_DB,
                        min_prior_pressers: int = 20) -> dict:
    """For each presser P with ≥min_prior, predict each word's probability
    using corpus [1..P-1], then score against actual(P).

    Output: list of (predicted_prob, actually_said) tuples + calibration buckets.
    """
    pressers = load_all_pressers(db_path)
    _log.info(f"Loaded {len(pressers)} historical FOMC pressers")

    if len(pressers) < min_prior_pressers + 5:
        return {"error": f"not enough pressers: {len(pressers)}"}

    base_rate_fn = make_base_rate_fn(db_path)

    predictions: list[tuple[float, bool, str, str]] = []
    # (predicted_prob, actually_said, word, event_date)
    for i, target in enumerate(pressers):
        if i < min_prior_pressers:
            continue
        # Compile corpus up to (but not including) target
        event_date = target["event_date"]
        pkg = compile_for_event(
            event_id=f"backtest_{event_date}",
            event_type="fomc_presser",
            event_date_iso=event_date,
            db_path=db_path,
            prior_n=min(i, 40),
            include_statements=False,
            include_minutes=False,
        )
        target_text = target["raw_text"]

        for _suffix, word_raw, variants in CANONICAL_TERMS:
            # Base rate from corpus
            raw_rate, k, n = base_rate_fn("fomc_presser", variants)
            # Apply Laplace smoothing like thesis_builder does at extremes
            if n == 0:
                base_rate = 0.5
            elif k == 0 or k == n:
                base_rate = (k + 1) / (n + 2)
            else:
                base_rate = raw_rate

            # Context adjustment from recent transcripts (heuristic)
            delta, _ = heuristic_context_scorer(word_raw, base_rate, pkg.recent_transcripts)

            # Apply logit + sigmoid
            import math
            def _logit(p): return math.log(p / (1 - p))
            def _sig(x): return 1 / (1 + math.exp(-x))
            adj_prob = _sig(_logit(max(0.001, min(0.999, base_rate))) + delta)

            actually = word_in_transcript(target_text, variants)
            predictions.append((adj_prob, actually, word_raw, event_date))

    # Calibration bucketing
    buckets = defaultdict(lambda: {"n": 0, "yes": 0})
    for prob, actual, _w, _d in predictions:
        bkt = min(9, int(prob * 10))   # 0..9
        buckets[bkt]["n"] += 1
        if actual:
            buckets[bkt]["yes"] += 1

    # Brier score
    brier = sum((p - (1 if a else 0)) ** 2 for p, a, _, _ in predictions) / max(1, len(predictions))

    return {
        "n_predictions": len(predictions),
        "n_pressers_tested": len(pressers) - min_prior_pressers,
        "n_terms": len(CANONICAL_TERMS),
        "brier_score": round(brier, 4),
        "calibration_buckets": {
            f"{b*10}-{b*10+10}%": {
                "n": buckets[b]["n"],
                "predicted_mid": b * 10 + 5,
                "actual_yes_rate": round(buckets[b]["yes"] / max(1, buckets[b]["n"]) * 100, 1),
                "yes": buckets[b]["yes"],
            }
            for b in range(10) if buckets[b]["n"] > 0
        },
    }


# ─── Mode 2: Kalshi replay (March 18 ground truth) ─────────────────────
def fetch_settled_markets_for_date(date_str: str) -> list[dict]:
    """Pull all settled KXFEDMENTION markets for a given close-date (YYYY-MM-DD)."""
    all_settled = []
    cursor = None
    attempts = 0
    while True:
        params = {"series_ticker": "KXFEDMENTION", "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI_BASE}/markets", params=params, timeout=10)
            if r.status_code == 429:
                attempts += 1
                if attempts < 5:
                    time.sleep(3)
                    continue
                break
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            _log.error(f"fetch failed: {e}")
            break
        batch = d.get("markets", []) or []
        all_settled.extend(batch)
        cursor = d.get("cursor", "") or ""
        if not cursor or not batch:
            break
        if len(all_settled) > 2000:
            break
        time.sleep(0.15)

    # Filter to target date
    matching = [m for m in all_settled
                if (m.get("close_time", "") or "").startswith(date_str)]
    return matching


def run_kalshi_backtest(event_date_iso: str,
                        bankroll: float = 5000.0,
                        db_path: str = SOVEREIGN_DB) -> dict:
    """Replay thesis generation for a historical FOMC date, compare to actuals."""
    markets_raw = fetch_settled_markets_for_date(event_date_iso)
    _log.info(f"Fetched {len(markets_raw)} settled markets for {event_date_iso}")

    if not markets_raw:
        return {"error": f"no settled markets for {event_date_iso}"}

    # Build corpus as of DAY BEFORE this presser
    pressers = load_all_pressers(db_path)
    target_presser = next((p for p in pressers if p["event_date"] == event_date_iso), None)
    if not target_presser:
        return {"error": f"no transcript for {event_date_iso}"}

    pkg = compile_for_event(
        event_id=f"backtest_{event_date_iso}",
        event_type="fomc_presser",
        event_date_iso=event_date_iso,
        db_path=db_path,
        prior_n=30,
    )

    # Parse each Kalshi market → MentionMarket
    from prep.term_expander import parse_title, parse_close_time
    markets: list[tuple[MentionMarket, str]] = []
    for m in markets_raw:
        parsed = parse_title(m.get("title", ""))
        if not parsed:
            continue
        word_raw, variants = parsed
        ct = parse_close_time(m.get("close_time", ""))
        if ct is None:
            continue
        markets.append((
            MentionMarket(
                ticker=m["ticker"], title=m.get("title", ""),
                word_raw=word_raw, word_variants=variants,
                close_time_utc=ct,
                yes_bid=m.get("yes_bid"), yes_ask=m.get("yes_ask"),
            ),
            (m.get("result") or "").lower(),
        ))

    # Run thesis for each
    base_rate_fn = make_base_rate_fn(db_path)
    target_text = target_presser["raw_text"]

    wins = losses = skipped = 0
    total_cost = total_pnl = 0.0
    trade_detail = []

    for m, outcome in markets:
        t = build_thesis(m, pkg, base_rate_fn, heuristic_context_scorer,
                          bankroll_usd=bankroll)
        if t.conviction == "SKIP":
            skipped += 1
            continue

        # Simulate: at illiquid entry, we'd pay ~50¢ per contract (no real prices yet)
        # For ground-truth, check if our predicted side matches actual outcome.
        assume_entry = 0.50   # illiquid fair-value default
        contracts = int(t.recommended_usd / assume_entry) if t.recommended_usd > 0 else 0
        if contracts == 0:
            skipped += 1
            continue

        cost = contracts * assume_entry
        total_cost += cost

        won = (t.best_side == "yes" and outcome == "yes") or \
              (t.best_side == "no" and outcome == "no")
        if won:
            # Pay out $1/contract, subtract cost
            pnl = contracts * (1.0 - assume_entry)
            wins += 1
        else:
            pnl = -cost
            losses += 1
        total_pnl += pnl
        trade_detail.append({
            "word": t.word_raw, "side": t.best_side, "edge": t.best_edge,
            "adjusted_prob": t.adjusted_prob, "base_rate": t.base_rate,
            "actual_outcome": outcome, "won": won,
            "contracts": contracts, "pnl": round(pnl, 2),
            "conviction": t.conviction,
        })

    n = wins + losses
    return {
        "event_date": event_date_iso,
        "markets_available": len(markets),
        "traded": n,
        "skipped": skipped,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 3) if n else 0,
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": round(total_pnl / total_cost, 3) if total_cost > 0 else 0,
        "detail": trade_detail,
    }


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["corpus", "kalshi", "both"], default="both")
    p.add_argument("--date", default="2026-03-18",
                   help="FOMC date for --mode kalshi (YYYY-MM-DD)")
    p.add_argument("--bankroll", type=float, default=5000.0)
    p.add_argument("--output-json", default=None)
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args()

    results = {}

    if a.mode in ("corpus", "both"):
        print("\n══════════════════════════════════════════════════════")
        print("  MODE: CORPUS CALIBRATION (across 74 pressers)")
        print("══════════════════════════════════════════════════════")
        r = run_corpus_backtest()
        results["corpus"] = r
        print(f"  Predictions:        {r['n_predictions']}")
        print(f"  Pressers tested:    {r['n_pressers_tested']}")
        print(f"  Terms per presser:  {r['n_terms']}")
        print(f"  Brier score:        {r['brier_score']}  (lower=better, 0.25=random)")
        print()
        print(f"  {'prob bucket':<14s} {'n':>5s} {'predicted':>10s} {'actual':>8s}")
        for bkt, s in sorted(r["calibration_buckets"].items()):
            print(f"  {bkt:<14s} {s['n']:>5d} {s['predicted_mid']:>9d}% {s['actual_yes_rate']:>7.1f}%")

    if a.mode in ("kalshi", "both"):
        print(f"\n══════════════════════════════════════════════════════")
        print(f"  MODE: KALSHI REPLAY ({a.date})")
        print(f"══════════════════════════════════════════════════════")
        r = run_kalshi_backtest(a.date, bankroll=a.bankroll)
        results["kalshi"] = r
        if "error" in r:
            print(f"  ERROR: {r['error']}")
        else:
            print(f"  Markets available:  {r['markets_available']}")
            print(f"  Traded / Skipped:   {r['traded']} / {r['skipped']}")
            print(f"  Wins / Losses:      {r['wins']} / {r['losses']}")
            print(f"  Win rate:           {r['win_rate']*100:.1f}%")
            print(f"  Total cost:         ${r['total_cost']:.2f}")
            print(f"  Total PnL:          ${r['total_pnl']:+.2f}")
            print(f"  ROI:                {r['roi']*100:+.1f}%")
            if a.verbose:
                print()
                print("  Per-trade detail:")
                for t in r["detail"][:30]:
                    mark = "✓" if t["won"] else "✗"
                    print(f"   {mark} {t['word'][:26]:<28s} {t['side']:<3s} "
                          f"prob={t['adjusted_prob']*100:>5.1f}% "
                          f"edge={t['edge']*100:>+5.1f}% "
                          f"→ {t['actual_outcome']:<3s} PnL=${t['pnl']:+.2f}")

    if a.output_json:
        with open(a.output_json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n  JSON → {a.output_json}")


if __name__ == "__main__":
    main()
