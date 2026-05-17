"""Passive LIP observer.

Connects the live Kalshi WS feed to our scorer, WITHOUT placing any orders.
Measures per-market:
  - How often snapshots are 'valid' (both sides meet TargetSize)
  - Typical total qualifying score per side
  - Number of competing quoters (inferable from qualifying-score distribution)
  - Estimated payout if we captured X% share

This is the foundation for: (a) validating our scoring math against live books,
(b) deciding which markets are WORTH quoting, (c) measuring the opportunity
before we commit capital.

Run it:
    venv/bin/python monitor/passive_observer.py
    (or with PYTHONPATH=. python monitor/passive_observer.py)

Runs until Ctrl-C. Prints per-market summary every 30 seconds.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine.lip_discovery import top_n_to_quote, discover
from engine.lip_scorer import (
    OurQuotes, ProgramParams, score_snapshot, estimated_period_payout,
)
from execution.kalshi_ws import KalshiWS, BookState

_log = logging.getLogger(__name__)


class PassiveObserver:
    def __init__(self, markets: list[dict]):
        self.markets = markets  # list of dicts from top_n_to_quote
        self.params_by_ticker = {
            m["market_ticker"]: ProgramParams(
                market_ticker=m["market_ticker"],
                target_size=int(m["target_size"]),
                discount_factor=float(m["discount_factor"]),
                period_reward_usd=float(m["reward_per_day_usd"]),  # treat daily reward as period_reward
            )
            for m in markets
        }
        # Per-market running stats
        self.valid_snaps    = defaultdict(int)  # count of valid (2-sided qualifying)
        self.invalid_snaps  = defaultdict(int)
        self.total_snaps    = defaultdict(int)
        self.yes_score_sum  = defaultdict(float)  # sum of total_qualifying yes score (proxy for depth)
        self.no_score_sum   = defaultdict(float)
        self.yes_score_n    = defaultdict(int)
        self.no_score_n     = defaultdict(int)
        self.last_scored_at = defaultdict(float)  # throttle scoring to ~1/sec
        self.start_time     = time.time()

    async def on_book_update(self, book: BookState):
        """WS callback — score with OUR empty quotes to measure book state."""
        params = self.params_by_ticker.get(book.market_ticker)
        if params is None:
            return
        now = time.time()
        # Throttle: score each market at most once per second
        if now - self.last_scored_at[book.market_ticker] < 1.0:
            return
        self.last_scored_at[book.market_ticker] = now

        # Score with NO quotes of our own — measures book state only
        r = score_snapshot(book, OurQuotes(), params)
        self.total_snaps[book.market_ticker] += 1
        if r.snapshot_valid:
            self.valid_snaps[book.market_ticker] += 1
            self.yes_score_sum[book.market_ticker] += r.yes_total_qualifying_score
            self.no_score_sum[book.market_ticker]  += r.no_total_qualifying_score
            self.yes_score_n[book.market_ticker]   += 1
            self.no_score_n[book.market_ticker]    += 1
        else:
            self.invalid_snaps[book.market_ticker] += 1

    def print_summary(self):
        elapsed = time.time() - self.start_time
        print(f"\n=== Observer summary ({elapsed:.0f}s elapsed) ===")
        print(f"{'market':40s} {'valid':>5s} {'inval':>5s} {'valid%':>6s} {'avg_yes_qsz':>11s} {'avg_no_qsz':>10s} {'$/day pool':>10s}")
        total_reward_pool = 0.0
        for m in self.markets:
            tkr = m["market_ticker"]
            total = self.total_snaps.get(tkr, 0)
            valid = self.valid_snaps.get(tkr, 0)
            invalid = self.invalid_snaps.get(tkr, 0)
            pct = (valid / total * 100) if total else 0.0
            avg_yes = (self.yes_score_sum[tkr] / self.yes_score_n[tkr]) if self.yes_score_n[tkr] else 0
            avg_no  = (self.no_score_sum[tkr] / self.no_score_n[tkr]) if self.no_score_n[tkr] else 0
            daily = m["reward_per_day_usd"]
            total_reward_pool += daily
            print(f"  {tkr[:38]:38s} {valid:>5d} {invalid:>5d} {pct:>5.1f}% {avg_yes:>11.0f} {avg_no:>10.0f} ${daily:>8.2f}")
        valid_count = sum(self.valid_snaps.values())
        total_count = sum(self.total_snaps.values())
        overall_valid_pct = (valid_count / total_count * 100) if total_count else 0
        print(f"\n  OVERALL: {valid_count}/{total_count} valid snapshots = {overall_valid_pct:.1f}%")
        print(f"  Reward pool across these markets: ${total_reward_pool:.2f}/day")
        if valid_count > 0:
            # Rough estimate: if we quote size X on a market with avg_total_score S,
            # our snapshot share ≈ X / (X + S). Summed across all valid snapshots,
            # our period score ≈ num_valid × share. Simulate at 20% share.
            for share_pct in [5, 10, 20, 30]:
                est_day = total_reward_pool * (share_pct / 100) * (overall_valid_pct / 100)
                print(f"  at {share_pct:>2d}% share × {overall_valid_pct:.0f}% valid = ~${est_day:>7.2f}/day = ~${est_day*30:>6.0f}/month")


async def run(duration_sec: int = 120):
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

    # Refresh programs first
    print("refreshing LIP program inventory...")
    discover(save=True)
    markets = top_n_to_quote(15)  # top-15 — don't overload WS first run
    if not markets:
        print("no enrolled markets — tune config/settings.py filters")
        return
    print(f"observing {len(markets)} markets for {duration_sec}s...")
    for m in markets:
        print(f"  {m['market_ticker']:40s} target={m['target_size']:>5.0f} df={m['discount_factor']:.2f} ${m['reward_per_day_usd']:.2f}/day")

    obs = PassiveObserver(markets)
    ws = KalshiWS()
    await ws.connect()
    ws.on_update(obs.on_book_update)
    await ws.subscribe_orderbook([m["market_ticker"] for m in markets])

    # Run for duration, printing summary every 30s
    stop_event = asyncio.Event()
    def _stop():
        stop_event.set()
    # Ctrl-C support
    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
    except NotImplementedError:
        pass  # windows

    ws_task = asyncio.create_task(ws.run())
    deadline = time.time() + duration_sec
    try:
        while time.time() < deadline and not stop_event.is_set():
            await asyncio.sleep(30)
            obs.print_summary()
    finally:
        await ws.close()
        try:
            await asyncio.wait_for(ws_task, timeout=2)
        except Exception:
            pass

    obs.print_summary()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=120, help="seconds to observe")
    a = p.parse_args()
    asyncio.run(run(duration_sec=a.duration))
