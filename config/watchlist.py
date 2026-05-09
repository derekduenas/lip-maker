"""No-Corpus Watchlist — tracks markets found with no historical data."""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist.json")


def _load_watchlist() -> list[dict]:
    if os.path.exists(WATCHLIST_PATH):
        with open(WATCHLIST_PATH) as f:
            return json.load(f)
    return []


def _save_watchlist(entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def add_to_watchlist(event_type: str, ticker: str, term: str,
                     market_price: float, volume: float) -> None:
    """Add or update a market in the watchlist."""
    entries = _load_watchlist()

    # Check if already exists
    for entry in entries:
        if entry["event_type"] == event_type and entry["term"] == term:
            entry["seen_count"] += 1
            entry["market_price"] = market_price
            entry["volume"] = volume
            entry["last_seen"] = datetime.utcnow().strftime("%Y-%m-%d")
            _save_watchlist(entries)
            return

    entries.append({
        "event_type": event_type,
        "ticker": ticker,
        "term": term,
        "market_price": market_price,
        "volume": volume,
        "first_seen": datetime.utcnow().strftime("%Y-%m-%d"),
        "last_seen": datetime.utcnow().strftime("%Y-%m-%d"),
        "seen_count": 1,
    })
    _save_watchlist(entries)


def watchlist_report() -> None:
    """Print the current watchlist."""
    entries = _load_watchlist()
    if not entries:
        print("Watchlist is empty — no unknown markets encountered yet.")
        return

    # Sort by seen_count * volume descending (priority)
    entries.sort(key=lambda e: e.get("seen_count", 0) * e.get("volume", 0), reverse=True)

    print("\nCORPUS WATCHLIST — Markets found with no corpus data")
    print("=" * 80)
    print(f"{'Event Type':<25} | {'Ticker':<7} | {'Term':<15} | {'Price':>5} | {'Vol':>8} | {'Seen':>4}")
    print("-" * 80)

    tickers_needed = set()
    for e in entries[:20]:
        price_str = f"{e['market_price']*100:.0f}c" if e['market_price'] < 1 else f"{e['market_price']:.0f}c"
        print(f"{e['event_type']:<25} | {e.get('ticker','?'):<7} | {e['term']:<15} | {price_str:>5} | {e['volume']:>8.0f} | {e['seen_count']:>3}x")
        if e["seen_count"] >= 2 and e["volume"] > 100:
            tickers_needed.add(e.get("ticker", "?"))

    print("-" * 80)
    if tickers_needed:
        print(f"ACTION: Build corpus for: {', '.join(sorted(tickers_needed))}")
        for t in sorted(tickers_needed):
            print(f"  Run: python3 corpus/builder_earnings.py --ticker {t} --company {t}")
    print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.report:
        watchlist_report()
    else:
        watchlist_report()
