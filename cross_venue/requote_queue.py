"""Cross-venue requote queue — durable signal from capital_reaper to runners.

When the capital_reaper cancels a stale order, it appends a line here.
The runners drain this queue at the top of their cycle and immediately
re-quote those tickers (instead of waiting up to 30min for the next
periodic discovery pass).

Format: line-delimited JSON, one per cancellation:
  {"venue": "kalshi", "ticker": "KX...", "ts": "2026-...", "reason": "stale"}
  {"venue": "pm",     "slug":   "tc-...", "ts": "2026-...", "reason": "stale"}

Drain operation reads + truncates atomically (rename to .processing).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

QUEUE_PATH = Path("/root/state/requote_queue.jsonl")


def _ensure_dir() -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)


def enqueue(venue: str, key: str, reason: str = "stale") -> None:
    """Append a cancellation. `key` = ticker (kalshi) or slug (pm)."""
    _ensure_dir()
    payload = {
        "venue":  venue,
        "key":    key,
        "ts":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason,
    }
    with QUEUE_PATH.open("a") as f:
        f.write(json.dumps(payload) + "\n")


def drain(venue_filter: str | None = None) -> list[dict]:
    """Read + clear the queue. Optionally filter by venue. Atomic via rename."""
    _ensure_dir()
    if not QUEUE_PATH.exists():
        return []
    # Atomic move so we don't lose writes during read
    tmp = QUEUE_PATH.with_suffix(".processing")
    try:
        os.rename(QUEUE_PATH, tmp)
    except FileNotFoundError:
        return []
    entries: list[dict] = []
    with tmp.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if venue_filter and d.get("venue") != venue_filter:
                    continue
                entries.append(d)
            except json.JSONDecodeError:
                continue
    tmp.unlink(missing_ok=True)
    return entries
