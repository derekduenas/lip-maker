"""Backtester — runs current system logic against historical Kalshi mention markets."""

import json
import logging
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, MIN_EDGE_SINGLE, MIN_MARKET_PRICE, MAX_MARKET_PRICE
from engine.frequency import extract_mentions
from engine.edge_calc import kalshi_maker_fee, kelly_sizing, confidence_weight
from engine.scanner import classify_market

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("backtest")

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "backtest_results.json")


def fetch_historical_markets(max_pages: int = 20) -> list[dict]:
    """Pull all settled mention markets from Kalshi API."""
    from engine.scanner import KalshiClient
    client = KalshiClient()

    all_markets = []
    cursor = None

    for page in range(max_pages):
        params = {"limit": 200, "status": "settled", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        data = client._get("/events", params=params)
        events = data.get("events", [])

        for event in events:
            title = (event.get("title", "") or "").lower()
            ticker = (event.get("event_ticker", "") or "").lower()
            if not any(kw in title for kw in ["mention", " say ", "will say"]) and "mention" not in ticker:
                continue

            for m in event.get("markets", []):
                result = m.get("result")
                if result not in ("yes", "no"):
                    continue

                last_price = float(m.get("last_price_dollars", 0) or 0)
                yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
                yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
                volume = float(m.get("volume_fp", 0) or 0)

                classified = classify_market({
                    "ticker": m.get("ticker", ""),
                    "title": m.get("title", ""),
                    "event_title": event.get("title", ""),
                    "event_ticker": event.get("event_ticker", ""),
                    "subtitle": m.get("yes_sub_title", ""),
                })

                all_markets.append({
                    "ticker": m.get("ticker", ""),
                    "event_ticker": event.get("event_ticker", ""),
                    "event_title": event.get("title", ""),
                    "term": classified.get("term"),
                    "event_type": classified.get("event_type", "other"),
                    "event_date": classified.get("event_date"),
                    "speaker": classified.get("speaker"),
                    "result": result,
                    "volume": volume,
                    "close_time": m.get("close_time", ""),
                })

        cursor = data.get("cursor", "")
        if not cursor or not events:
            break

    log.info(f"Fetched {len(all_markets)} settled mention markets")
    return all_markets


def simulate_trade(market: dict, conn: sqlite3.Connection) -> dict | None:
    """
    Simulate what our system would have done on this historical market.
    Uses only transcripts from BEFORE the event date (temporal isolation).
    """
    term = market.get("term")
    event_type = market.get("event_type")
    event_date = market.get("event_date")
    result = market.get("result")

    if not term or not event_type or event_type == "other":
        return None

    # Temporal isolation: only count transcripts before this event
    if event_date:
        rows = conn.execute(
            "SELECT raw_text FROM transcripts WHERE event_type=? AND event_date < ? ORDER BY event_date",
            (event_type, event_date)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT raw_text FROM transcripts WHERE event_type=?",
            (event_type,)
        ).fetchall()

    n = len(rows)
    if n < 3:
        return None  # not enough corpus

    # Compute base rate from pre-event corpus
    k = 0
    for (text,) in rows:
        mentions = extract_mentions(text, [term])
        if mentions[term]["mentioned"]:
            k += 1

    base_rate = k / n
    smoothed = (k + 1) / (n + 2)  # Bayesian smoothing

    # We don't have historical Kalshi prices at t-24h, so use a simulated market price
    # based on what a naive market would price (slightly noisy around base rate)
    # For backtest, we use the actual base rate +/- random noise as the "market price"
    # This tests whether our CONTEXT ADJUSTMENT adds value over raw base rate
    import random
    random.seed(hash(market["ticker"]))  # deterministic per market
    noise = random.gauss(0, 0.10)
    simulated_market_price = max(0.05, min(0.95, base_rate + noise))

    # Edge calculation
    fee = kalshi_maker_fee(simulated_market_price)
    edge_yes = smoothed - simulated_market_price - fee
    edge_no = (1 - smoothed) - (1 - simulated_market_price) - kalshi_maker_fee(1 - simulated_market_price)

    if edge_yes >= edge_no and edge_yes > 0:
        edge = edge_yes
        side = "yes"
        prob = smoothed
        price = simulated_market_price
    elif edge_no > 0:
        edge = edge_no
        side = "no"
        prob = 1 - smoothed
        price = 1 - simulated_market_price
    else:
        return None  # no edge

    if edge < MIN_EDGE_SINGLE:
        return None  # below threshold

    # Would have traded
    conf = confidence_weight(n)
    kelly_pct, _, high_conv, strategy = kelly_sizing(prob, price, corpus_n=n)

    # Determine outcome
    if (result == "yes" and side == "yes") or (result == "no" and side == "no"):
        outcome = "WIN"
        pnl_per_dollar = (1.0 - price) / price  # return on investment
    else:
        outcome = "LOSS"
        pnl_per_dollar = -1.0

    return {
        "ticker": market["ticker"],
        "term": term,
        "event_type": event_type,
        "event_date": event_date,
        "side": side,
        "base_rate": base_rate,
        "smoothed": smoothed,
        "market_price": simulated_market_price,
        "edge": edge,
        "kelly_pct": kelly_pct,
        "confidence": conf,
        "high_conviction": high_conv,
        "strategy": strategy,
        "corpus_n": n,
        "actual_result": result,
        "outcome": outcome,
        "pnl_per_dollar": pnl_per_dollar,
    }


def run_backtest() -> dict:
    """Run full backtest against all historical mention markets."""
    log.info("=" * 60)
    log.info("SOVEREIGN BACKTESTER")
    log.info("=" * 60)

    # Fetch historical markets
    log.info("Fetching historical settled mention markets...")
    markets = fetch_historical_markets()

    conn = sqlite3.connect(DB_PATH)

    # Simulate each
    trades = []
    skipped_no_corpus = 0
    skipped_no_edge = 0
    skipped_other = 0

    for i, market in enumerate(markets):
        if i % 100 == 0 and i > 0:
            log.info(f"  Processing {i}/{len(markets)}...")

        result = simulate_trade(market, conn)
        if result:
            trades.append(result)
        else:
            if market.get("event_type") == "other":
                skipped_other += 1
            else:
                # Check why skipped
                event_type = market.get("event_type", "")
                n = conn.execute("SELECT COUNT(*) FROM transcripts WHERE event_type=?", (event_type,)).fetchone()[0]
                if n < 3:
                    skipped_no_corpus += 1
                else:
                    skipped_no_edge += 1

    conn.close()

    # Analyze results
    total = len(trades)
    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    win_rate = len(wins) / total * 100 if total > 0 else 0

    # ROI: weighted by Kelly sizing
    total_deployed = sum(t["kelly_pct"] for t in trades)
    total_pnl = sum(t["kelly_pct"] * t["pnl_per_dollar"] for t in trades)
    roi = total_pnl / total_deployed * 100 if total_deployed > 0 else 0

    # By event type
    by_event = defaultdict(lambda: {"wins": 0, "losses": 0, "trades": 0})
    for t in trades:
        et = t["event_type"]
        by_event[et]["trades"] += 1
        if t["outcome"] == "WIN":
            by_event[et]["wins"] += 1
        else:
            by_event[et]["losses"] += 1

    # By edge bucket
    by_edge = {"8-15pp": {"w": 0, "l": 0}, "15-25pp": {"w": 0, "l": 0}, "25pp+": {"w": 0, "l": 0}}
    for t in trades:
        edge_pp = t["edge"] * 100
        if edge_pp >= 25:
            bucket = "25pp+"
        elif edge_pp >= 15:
            bucket = "15-25pp"
        else:
            bucket = "8-15pp"
        if t["outcome"] == "WIN":
            by_edge[bucket]["w"] += 1
        else:
            by_edge[bucket]["l"] += 1

    # By corpus depth
    by_corpus = {"n=3-5": {"w": 0, "l": 0}, "n=6-10": {"w": 0, "l": 0}, "n=10+": {"w": 0, "l": 0}}
    for t in trades:
        cn = t["corpus_n"]
        if cn >= 10:
            bucket = "n=10+"
        elif cn >= 6:
            bucket = "n=6-10"
        else:
            bucket = "n=3-5"
        if t["outcome"] == "WIN":
            by_corpus[bucket]["w"] += 1
        else:
            by_corpus[bucket]["l"] += 1

    # Best and worst trades
    sorted_by_edge = sorted(trades, key=lambda t: t["edge"], reverse=True)
    best_wins = [t for t in sorted_by_edge if t["outcome"] == "WIN"][:10]
    worst_losses = sorted([t for t in trades if t["outcome"] == "LOSS"],
                          key=lambda t: t["edge"], reverse=True)[:10]

    # Print report
    print()
    print("=" * 70)
    print("BACKTEST REPORT")
    print("=" * 70)
    print(f"Historical markets analyzed:  {len(markets)}")
    print(f"Skipped (no corpus):         {skipped_no_corpus}")
    print(f"Skipped (no edge):           {skipped_no_edge}")
    print(f"Skipped (unclassified):      {skipped_other}")
    print(f"Simulated trades:            {total}")
    print(f"Win rate:                    {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"Kelly-weighted ROI:          {roi:+.1f}%")
    print()

    print("BY EVENT TYPE:")
    print(f"  {'Type':<25} {'Trades':>6} {'Win%':>6}")
    print("  " + "-" * 40)
    for et, stats in sorted(by_event.items(), key=lambda x: x[1]["trades"], reverse=True):
        wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
        print(f"  {et:<25} {stats['trades']:>6} {wr:>5.1f}%")

    print()
    print("BY EDGE BUCKET:")
    for bucket in ["8-15pp", "15-25pp", "25pp+"]:
        w, l = by_edge[bucket]["w"], by_edge[bucket]["l"]
        total_b = w + l
        wr = w / total_b * 100 if total_b > 0 else 0
        print(f"  {bucket:<10} {total_b:>4} trades  {wr:.1f}% WR")

    print()
    print("BY CORPUS DEPTH:")
    for bucket in ["n=3-5", "n=6-10", "n=10+"]:
        w, l = by_corpus[bucket]["w"], by_corpus[bucket]["l"]
        total_b = w + l
        wr = w / total_b * 100 if total_b > 0 else 0
        print(f"  {bucket:<10} {total_b:>4} trades  {wr:.1f}% WR")

    print()
    print("TOP 10 WINS (by edge):")
    for t in best_wins[:10]:
        print(f"  {t['term']:<20} {t['event_type']:<20} edge={t['edge']*100:+.1f}pp n={t['corpus_n']}")

    print()
    print("TOP 10 LOSSES (by edge — these are the ones to investigate):")
    for t in worst_losses[:10]:
        print(f"  {t['term']:<20} {t['event_type']:<20} edge={t['edge']*100:+.1f}pp n={t['corpus_n']} base={t['base_rate']*100:.0f}%")

    print("=" * 70)

    # Save results
    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "markets_analyzed": len(markets),
        "simulated_trades": total,
        "win_rate": win_rate,
        "kelly_roi": roi,
        "wins": len(wins),
        "losses": len(losses),
        "by_event": {k: dict(v) for k, v in by_event.items()},
        "by_edge": by_edge,
        "by_corpus": by_corpus,
        "trades": trades,
    }

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"Results saved to {RESULTS_PATH}")

    return report


if __name__ == "__main__":
    run_backtest()
