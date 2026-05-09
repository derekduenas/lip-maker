"""Live press-conference monitor for Sovereign FOMC events.

During the 90-min window around a Fed press conference, polls all
KXFEDMENTION markets every 30s and detects "word said" events via price
jumps. Kalshi's markets react to transcript content faster than most
transcript APIs publish — so the market itself is our best real-time feed.

Three outputs:
  1. Price-movement log (every poll, all markets) → CSV/DB
  2. "Word-said" events (YES price crossing high threshold) → alerts
  3. Live P&L tracker for our thesis positions → console + log

Run standalone:
    PYTHONPATH=. python3 prep/live_monitor.py \\
        --event fomc_20260429 --duration-min 90

Or auto-triggered by scheduler at T=0 milestone.

Detection rules:
  - YES price ≥ 0.85 → "Powell SAID the word" (high confidence)
  - YES price ≤ 0.05 → "Powell DID NOT say the word" (after enough time)
  - Sudden jump ≥ 0.20 in 60s → flag for attention (word likely said)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_calendar.events import get_event

_log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ─── Detection thresholds ─────────────────────────────────────────
SAID_HIGH_CONF     = 85   # yes_price ≥ 85c → almost certainly said
NOT_SAID_HIGH_CONF = 5    # yes_price ≤ 5c → almost certainly not said (after warmup)
JUMP_THRESHOLD     = 20   # ≥ 20c move in single tick → likely transition event
WARMUP_MINUTES     = 5    # don't fire NOT_SAID alerts in first 5 min


@dataclass
class MarketSnapshot:
    ticker:       str
    yes_bid:      Optional[int]
    yes_ask:      Optional[int]
    volume:       Optional[int]
    captured_at:  float

    def yes_mid(self) -> Optional[float]:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2.0
        return self.yes_ask or self.yes_bid


@dataclass
class WordEvent:
    ticker:    str
    word:      str          # parsed from title
    event:     str          # "SAID" | "NOT_SAID" | "JUMP"
    price:     int          # yes_price at event time
    jump:      Optional[int] # delta in cents if JUMP
    detected_at: float
    our_side:  Optional[str]  # "yes" | "no" | None — what we positioned
    our_cost:  Optional[float]


def _parse_word(title: str) -> Optional[str]:
    """Extract word from 'Will Powell say X at his Apr 2026 press conference?'."""
    m = re.search(r"Will Powell say\s+(.+?)\s+at his .+ press conference\?",
                  title, re.IGNORECASE)
    return m.group(1).strip() if m else None


def poll_markets(series_ticker: str = "KXFEDMENTION") -> list[dict]:
    """Pull all open markets for a series. Public endpoint, no auth."""
    all_markets: list[dict] = []
    cursor = None
    attempts = 0
    while True:
        params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI_BASE}/markets", params=params, timeout=10)
            if r.status_code == 429:
                attempts += 1
                if attempts < 3:
                    time.sleep(2)
                    continue
                break
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            _log.warning(f"poll failed: {e}")
            break
        batch = d.get("markets", []) or []
        all_markets.extend(batch)
        cursor = d.get("cursor", "") or ""
        if not cursor or len(batch) < 200:
            break
    return all_markets


def _schema(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sovereign_live_ticks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            yes_bid      INTEGER,
            yes_ask      INTEGER,
            volume       INTEGER,
            captured_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lt_ticker_ts
            ON sovereign_live_ticks(ticker, captured_at);
        CREATE INDEX IF NOT EXISTS idx_lt_event
            ON sovereign_live_ticks(event_id);

        CREATE TABLE IF NOT EXISTS sovereign_live_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            word         TEXT,
            event_type   TEXT NOT NULL,
            price        INTEGER,
            jump         INTEGER,
            detected_at  REAL NOT NULL,
            our_side     TEXT,
            our_cost_usd REAL
        );
        CREATE INDEX IF NOT EXISTS idx_le_event
            ON sovereign_live_events(event_id);
        """)
        conn.commit()
    finally:
        conn.close()


def _log_tick(db_path: str, event_id: str, snap: MarketSnapshot):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO sovereign_live_ticks
               (event_id, ticker, yes_bid, yes_ask, volume, captured_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, snap.ticker, snap.yes_bid, snap.yes_ask,
             snap.volume, snap.captured_at),
        )
        conn.commit()
    finally:
        conn.close()


def _log_event(db_path: str, event_id: str, evt: WordEvent):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO sovereign_live_events
               (event_id, ticker, word, event_type, price, jump,
                detected_at, our_side, our_cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, evt.ticker, evt.word, evt.event, evt.price,
             evt.jump, evt.detected_at, evt.our_side, evt.our_cost),
        )
        conn.commit()
    finally:
        conn.close()


def load_our_positions(event_id: str, db_path: str) -> dict[str, dict]:
    """Load our placed orders for this event from sovereign_orders."""
    out: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                """SELECT ticker, side, price_cents, contracts, cost_usd, status
                   FROM sovereign_orders
                   WHERE event_id = ? AND status IN ('accepted', 'paper')""",
                (event_id,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {}
    for tkr, side, price, contracts, cost, status in rows:
        out[tkr] = {
            "side": side, "price_cents": price,
            "contracts": contracts, "cost_usd": cost, "status": status,
        }
    return out


# ─── Detection logic ──────────────────────────────────────────────
def classify_tick(
    prev: Optional[MarketSnapshot],
    curr: MarketSnapshot,
    warmup: bool,
) -> Optional[tuple[str, int, Optional[int]]]:
    """Return (event_type, price, jump) if anything notable, else None."""
    mid = curr.yes_mid()
    if mid is None:
        return None

    jump = None
    if prev and prev.yes_mid() is not None:
        jump = int(round(mid - prev.yes_mid()))

    # SAID detection — high confidence
    if mid >= SAID_HIGH_CONF:
        if prev is None or (prev.yes_mid() or 0) < SAID_HIGH_CONF:
            return ("SAID", int(round(mid)), jump)

    # NOT_SAID — require warmup + stable low
    if not warmup and mid <= NOT_SAID_HIGH_CONF:
        if prev is None or (prev.yes_mid() or 0) > NOT_SAID_HIGH_CONF:
            return ("NOT_SAID", int(round(mid)), jump)

    # JUMP — big move in one tick that doesn't cross a threshold
    if jump is not None and abs(jump) >= JUMP_THRESHOLD:
        return ("JUMP", int(round(mid)), jump)

    return None


def estimate_live_pnl(
    positions: dict[str, dict],
    current: dict[str, MarketSnapshot],
) -> dict:
    """Compute unrealized P&L of our positions at current prices.

    For a YES-side bet at entry_price P with C contracts:
      current_value = yes_mid × C / 100
      unrealized = current_value - cost
    """
    total_cost = 0.0
    total_value = 0.0
    realized_calls = 0     # positions already resolved (yes_mid=100 or 0)
    per_position = []

    for tkr, pos in positions.items():
        snap = current.get(tkr)
        if snap is None:
            continue
        mid = snap.yes_mid()
        if mid is None:
            continue
        # What we own: yes-contracts or no-contracts
        # In both cases, cost_usd is what we paid. Value depends on side.
        if pos["side"] == "yes":
            current_val = pos["contracts"] * mid / 100.0
        else:
            # NO contract: value = (100 - yes_mid) × contracts / 100
            current_val = pos["contracts"] * (100 - mid) / 100.0

        total_cost += pos["cost_usd"]
        total_value += current_val
        per_position.append({
            "ticker": tkr, "side": pos["side"],
            "entry": pos["price_cents"], "current": round(mid, 1),
            "contracts": pos["contracts"], "cost": pos["cost_usd"],
            "value": round(current_val, 2),
            "unrealized": round(current_val - pos["cost_usd"], 2),
        })
        if mid >= 98 or mid <= 2:
            realized_calls += 1

    return {
        "total_cost":      round(total_cost, 2),
        "total_value":     round(total_value, 2),
        "unrealized_pnl":  round(total_value - total_cost, 2),
        "n_positions":     len(positions),
        "n_likely_resolved": realized_calls,
        "per_position":    sorted(per_position,
                                   key=lambda p: -p["unrealized"]),
    }


# ─── Main loop ─────────────────────────────────────────────────────
def run_monitor(
    event_id: str,
    duration_min: int,
    poll_interval_sec: int = 30,
    db_path: str = "/root/sovereign/data/sovereign.db",
    kalshi_series: str = "KXFEDMENTION",
):
    """Main loop. Polls markets every poll_interval_sec for duration_min."""
    _schema(db_path)
    event = get_event(event_id)
    if event is None:
        _log.error(f"no such event: {event_id}")
        return

    positions = load_our_positions(event_id, db_path)
    _log.info(f"Loaded {len(positions)} placed positions for {event_id}")

    start_ts = time.time()
    end_ts = start_ts + duration_min * 60
    prev_snapshots: dict[str, MarketSnapshot] = {}
    tick_count = 0
    event_count = 0

    print(f"\n══════════════════════════════════════════════════════")
    print(f"  SOVEREIGN LIVE MONITOR — {event_id}")
    print(f"  Duration: {duration_min} min | Poll: {poll_interval_sec}s")
    print(f"  Positions: {len(positions)} | Warmup: {WARMUP_MINUTES} min")
    print(f"══════════════════════════════════════════════════════\n")

    while time.time() < end_ts:
        tick_start = time.time()
        tick_count += 1
        warmup_active = (time.time() - start_ts) < WARMUP_MINUTES * 60

        # Poll all markets
        raw = poll_markets(kalshi_series)
        current_snapshots: dict[str, MarketSnapshot] = {}
        for m in raw:
            tkr = m.get("ticker", "")
            if not tkr:
                continue
            snap = MarketSnapshot(
                ticker=tkr,
                yes_bid=m.get("yes_bid"),
                yes_ask=m.get("yes_ask"),
                volume=m.get("volume"),
                captured_at=time.time(),
            )
            current_snapshots[tkr] = snap
            _log_tick(db_path, event_id, snap)

            # Detect events
            prev = prev_snapshots.get(tkr)
            classified = classify_tick(prev, snap, warmup=warmup_active)
            if classified:
                event_type, price, jump = classified
                word = _parse_word(m.get("title", "")) or tkr
                our_pos = positions.get(tkr, {})
                evt = WordEvent(
                    ticker=tkr, word=word, event=event_type,
                    price=price, jump=jump,
                    detected_at=time.time(),
                    our_side=our_pos.get("side"),
                    our_cost=our_pos.get("cost_usd"),
                )
                _log_event(db_path, event_id, evt)
                event_count += 1
                # Highlight events involving our positions
                our_mark = ""
                if our_pos:
                    # Did this event confirm our bet?
                    if event_type == "SAID" and our_pos.get("side") == "yes":
                        our_mark = " ✅ OUR YES HIT"
                    elif event_type == "NOT_SAID" and our_pos.get("side") == "no":
                        our_mark = " ✅ OUR NO HIT"
                    elif event_type == "SAID" and our_pos.get("side") == "no":
                        our_mark = " ❌ OUR NO MISSED"
                    elif event_type == "NOT_SAID" and our_pos.get("side") == "yes":
                        our_mark = " ❌ OUR YES MISSED"
                jump_s = f" ({jump:+d}c)" if jump else ""
                print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                      f"{event_type:<9s} {word[:30]:<32s} @ {price:>3d}c{jump_s}{our_mark}")

        prev_snapshots = current_snapshots

        # Periodic P&L snapshot (every 5 ticks ≈ 2.5 min)
        if positions and tick_count % 5 == 0:
            pnl = estimate_live_pnl(positions, current_snapshots)
            print(f"\n  [P&L {datetime.now(timezone.utc).strftime('%H:%M')}] "
                  f"cost=${pnl['total_cost']:.2f} value=${pnl['total_value']:.2f} "
                  f"unrealized=${pnl['unrealized_pnl']:+.2f} "
                  f"resolved={pnl['n_likely_resolved']}/{pnl['n_positions']}\n")

        # Sleep to maintain poll interval
        elapsed = time.time() - tick_start
        sleep_for = max(1.0, poll_interval_sec - elapsed)
        if time.time() + sleep_for > end_ts:
            break
        time.sleep(sleep_for)

    # Final summary
    print(f"\n══════════════════════════════════════════════════════")
    print(f"  MONITOR COMPLETE — {tick_count} ticks, {event_count} events")
    print(f"══════════════════════════════════════════════════════")
    if positions:
        final_pnl = estimate_live_pnl(positions, prev_snapshots)
        print(f"\n  Final P&L:")
        print(f"    cost:        ${final_pnl['total_cost']:.2f}")
        print(f"    value:       ${final_pnl['total_value']:.2f}")
        print(f"    unrealized:  ${final_pnl['unrealized_pnl']:+.2f}")
        print(f"    resolved:    {final_pnl['n_likely_resolved']}/{final_pnl['n_positions']}")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True)
    p.add_argument("--duration-min", type=int, default=90)
    p.add_argument("--poll-sec", type=int, default=30)
    p.add_argument("--db", default="/root/sovereign/data/sovereign.db")
    a = p.parse_args()
    run_monitor(a.event, duration_min=a.duration_min,
                poll_interval_sec=a.poll_sec, db_path=a.db)


if __name__ == "__main__":
    main()
