"""Continuous Fed + Trump mention scanner — runs OFF the calendar.

The original loop/monitor.run_pre_event_scan only fires when an event in
config/events.py is ≤ 24h away. That's correct for earnings (which must be
fired with the speaker's quarterly call), but wrong for FED + TRUMP markets:
those markets are always open for some forward window (next FOMC, next major
political address, weekly mention rolls), and our corpus has standing
predictive content for both speakers (powell+fomc_committee+trump = 188
transcripts as of 2026-05-10). Skipping them between earnings burns the data
asset.

This scanner pulls EVERY currently-open KXFEDMENTION-* / KXTRUMPMENTION-*
market and runs the same edge pipeline regardless of whether a scheduled
event is on the calendar.

  ticker prefix    speaker(s) used        event_type tag
  KXFEDMENTION     powell + fomc_committee fed_speech
  KXTRUMPMENTION   trump                   trump_speech

Per-domain calibration is tracked separately by filtering on event_type when
running the reviewer. Today both domains share the same gate
(MIN_EDGE_SINGLE / kelly cap), but we separate the *audit* so we can detect
e.g. powell-corpus drift without trump-corpus drift contaminating the signal.

USAGE:
  python -m loop.continuous_scan            # one cycle (cron-friendly)
  python -m loop.continuous_scan --json     # machine-readable
  python -m loop.continuous_scan --dry-run  # no paper trades, just log

Cron (deploy/sovereign-continuous.{timer,service}): every 6 hours.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH


# Speaker → corpus event_type mapping. The corpus stores transcripts under
# event_type strings; calculate_edge() looks up the frequency_matrix by
# event_type+term. For Fed markets, the existing classify_market() returns
# event_type='fed_speech' and speaker='powell'. We honour that — but also
# expose a fallback to fomc_committee when powell-only is thin.
DOMAIN_PREFIXES = {
    "fed":   "KXFEDMENTION",
    "trump": "KXTRUMPMENTION",
}


def _get_conn():
    return sqlite3.connect(DB_PATH, timeout=10.0)


def _domain_of(ticker: str) -> str | None:
    tk = (ticker or "").upper()
    for dom, pfx in DOMAIN_PREFIXES.items():
        if pfx in tk:
            return dom
    return None


def _close_date(close_time: str) -> str:
    """Extract YYYY-MM-DD from a Kalshi close_time ISO string."""
    if not close_time:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        return datetime.fromisoformat(close_time.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return close_time[:10]


def _has_open_position(conn: sqlite3.Connection, ticker: str) -> bool:
    """Don't double-up on the same ticker."""
    r = conn.execute(
        "SELECT 1 FROM trades WHERE kalshi_market_id=? AND outcome='OPEN' LIMIT 1",
        (ticker,),
    ).fetchone()
    return r is not None


def run_continuous_scan(*, dry_run: bool = False) -> dict:
    """Pull open Fed + Trump mention markets, run edge pipeline on each."""
    from engine.scanner import KalshiClient, classify_market
    from engine.edge_calc import calculate_edge, _log_opportunity
    from engine.executor import Executor

    client = KalshiClient()
    if not client.authenticated:
        return {"error": "kalshi_unauthenticated"}

    all_markets = client.get_mention_markets()
    by_domain: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for m in all_markets:
        dom = _domain_of(m.get("ticker", ""))
        if dom not in DOMAIN_PREFIXES:
            continue
        classified = classify_market(m)
        if classified.get("event_type") not in ("fed_speech", "trump_speech"):
            continue
        if not classified.get("term") or not classified.get("speaker"):
            continue
        by_domain[dom].append((m, classified))

    if not any(by_domain.values()):
        return {
            "scanned": len(all_markets), "fed_markets": 0, "trump_markets": 0,
            "qualifying": 0, "executed": 0, "skipped_open_position": 0,
            "skipped_no_edge": 0, "errors": 0, "dry_run": dry_run,
        }

    # Generic always-applicable headlines so context_scorer has *something* to
    # weight against (real headline ingestion lives elsewhere). Keep terse so
    # we don't bias the scorer.
    headlines = [
        "Markets watching Fed for next rate-policy signal",
        "Trump policy posture shifting on tariffs and trade",
        "Inflation and employment data driving macro narrative",
    ]

    executor = Executor()
    conn = _get_conn()
    qualifying = executed = skipped_pos = skipped_no_edge = errors = 0
    per_domain: dict[str, dict] = {}

    for dom, items in by_domain.items():
        d_qualify = d_exec = d_skip_pos = d_skip_no_edge = d_err = 0
        sample_log: list[dict] = []

        for market, classified in items:
            term       = classified["term"]
            event_type = classified["event_type"]
            ticker     = market.get("ticker", "")
            event_date = _close_date(market.get("close_time", ""))
            yes_price  = market.get("yes_price", 0.5)

            try:
                edge_result = calculate_edge(
                    term=term, event_type=event_type, event_date=event_date,
                    kalshi_ticker=ticker, yes_price=yes_price,
                    news_headlines=headlines,
                )
            except Exception as e:
                d_err += 1
                continue

            if not edge_result or edge_result.get("signal") == "SKIP":
                d_skip_no_edge += 1
                continue

            d_qualify += 1
            _log_opportunity(edge_result)

            if dry_run:
                sample_log.append({
                    "ticker": ticker, "term": term,
                    "yes_price": round(yes_price, 4),
                    "edge_pp": round(edge_result.get("edge", 0) * 100, 2),
                    "side": edge_result.get("trade_side"),
                    "signal": edge_result.get("signal"),
                })
                continue

            if _has_open_position(conn, ticker):
                d_skip_pos += 1
                continue

            try:
                trade = executor.execute_opportunity(edge_result)
            except Exception as e:
                d_err += 1
                continue
            if trade and not trade.get("skipped"):
                d_exec += 1
                sample_log.append({
                    "ticker": ticker, "term": term,
                    "side": edge_result.get("trade_side"),
                    "edge_pp": round(edge_result.get("edge", 0) * 100, 2),
                    "contracts": trade.get("contracts"),
                    "price": trade.get("price_dollars"),
                })

        per_domain[dom] = {
            "markets":            len(items),
            "qualifying":         d_qualify,
            "executed":           d_exec,
            "skipped_open_pos":   d_skip_pos,
            "skipped_no_edge":    d_skip_no_edge,
            "errors":             d_err,
            "sample":             sample_log[:5],
        }
        qualifying      += d_qualify
        executed        += d_exec
        skipped_pos     += d_skip_pos
        skipped_no_edge += d_skip_no_edge
        errors          += d_err

    conn.close()
    return {
        "ts":                       datetime.now(timezone.utc).isoformat(),
        "scanned":                  len(all_markets),
        "fed_markets":              len(by_domain.get("fed", [])),
        "trump_markets":            len(by_domain.get("trump", [])),
        "qualifying":               qualifying,
        "executed":                 executed,
        "skipped_open_position":    skipped_pos,
        "skipped_no_edge":          skipped_no_edge,
        "errors":                   errors,
        "dry_run":                  dry_run,
        "per_domain":               per_domain,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log opportunities but do not execute paper trades")
    a = ap.parse_args()

    res = run_continuous_scan(dry_run=a.dry_run)

    if a.json:
        print(json.dumps(res, indent=2, default=str))
        return 0

    if res.get("error"):
        print(f"\n  ERROR: {res['error']}")
        return 2

    print(f"\n━━━ SOVEREIGN CONTINUOUS — Fed + Trump ━━━")
    print(f"  ts:                {res['ts']}")
    print(f"  total_scanned:     {res['scanned']} mention markets")
    print(f"  fed_markets:       {res['fed_markets']}")
    print(f"  trump_markets:     {res['trump_markets']}")
    print(f"  qualifying_edges:  {res['qualifying']}")
    print(f"  executed:          {res['executed']}{'  (dry-run)' if res['dry_run'] else ''}")
    print(f"  skipped_open_pos:  {res['skipped_open_position']}")
    print(f"  skipped_no_edge:   {res['skipped_no_edge']}")
    print(f"  errors:            {res['errors']}")
    for dom, d in res.get("per_domain", {}).items():
        print(f"\n  [{dom.upper()}]  {d['qualifying']}/{d['markets']} qualifying  "
              f"executed={d['executed']}  errors={d['errors']}")
        for s in d.get("sample", []):
            print(f"    - {s.get('ticker','')[:48]:<48}  "
                  f"side={s.get('side','?')}  edge={s.get('edge_pp',0):+.2f}pp")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
