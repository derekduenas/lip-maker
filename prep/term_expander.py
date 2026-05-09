"""Term expander — queries live Kalshi for all mention markets on an event
and extracts the exact word(s) to score.

For FOMC: queries KXFEDMENTION series for markets closing within ±24h of
event_datetime_utc, parses market title to extract the word, returns list
of (ticker, word, rules_text) tuples.

The word extraction is critical — Kalshi uses formal titles like
"Will Powell say Yield Curve at his Apr 2026 press conference?" and we need
to map that to the canonical corpus term "yield curve" (lowercase, may have
multiple variants like "QE / Quantitative Easing").
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_log = logging.getLogger(__name__)

# Regex: "Will Powell say X at his Apr 2026 press conference?"
_TITLE_RE = re.compile(
    r"Will Powell say\s+(.+?)\s+at his .+ press conference\?",
    re.IGNORECASE,
)

# Rarely-seen words may have slash-variants in the title.
# E.g., "QE / Quantitative Easing" → both phrasings count.


@dataclass
class MentionMarket:
    """A single Kalshi mention market we can quote."""
    ticker:         str
    title:          str
    word_raw:       str       # as it appears in title (e.g., "QE / Quantitative Easing")
    word_variants:  list[str] # normalized list (e.g., ["qe", "quantitative easing"])
    close_time_utc: datetime
    yes_bid:        Optional[int] = None   # cents, None if no liquidity
    yes_ask:        Optional[int] = None
    volume:         Optional[int] = None
    liquidity:      Optional[int] = None


def parse_title(title: str) -> Optional[tuple[str, list[str]]]:
    """Extract the word being asked about.

    Returns (raw_word, list_of_variants) or None if title doesn't match.
    Handles slash-delimited variants: "QE / Quantitative Easing" → ["qe", "quantitative easing"]
    Handles forward-slash in phrase: "AI / Artificial Intelligence" → ["ai", "artificial intelligence"]
    """
    m = _TITLE_RE.search(title)
    if not m:
        return None
    raw = m.group(1).strip()
    # Split on " / " variants
    parts = [p.strip().lower() for p in re.split(r"\s*/\s*", raw) if p.strip()]
    return raw, parts


def parse_close_time(close_time_iso: str) -> Optional[datetime]:
    """Parse ISO-format close time into UTC datetime."""
    if not close_time_iso:
        return None
    try:
        if close_time_iso.endswith("Z"):
            close_time_iso = close_time_iso[:-1] + "+00:00"
        return datetime.fromisoformat(close_time_iso)
    except (ValueError, TypeError):
        return None


def filter_markets_for_event(
    markets_json: list[dict],
    event_datetime_utc: datetime,
    tolerance_hours: int = 24,
) -> list[MentionMarket]:
    """Given raw market dicts from Kalshi API, filter to only those that
    match the target event (close_time within tolerance of event_datetime).
    """
    out: list[MentionMarket] = []
    lo = event_datetime_utc - timedelta(hours=tolerance_hours)
    hi = event_datetime_utc + timedelta(hours=tolerance_hours)

    for m in markets_json:
        title = m.get("title", "")
        parsed = parse_title(title)
        if parsed is None:
            continue
        word_raw, variants = parsed
        ct = parse_close_time(m.get("close_time", ""))
        if ct is None or not (lo <= ct <= hi):
            continue
        out.append(MentionMarket(
            ticker=m.get("ticker", ""),
            title=title,
            word_raw=word_raw,
            word_variants=variants,
            close_time_utc=ct,
            yes_bid=m.get("yes_bid"),
            yes_ask=m.get("yes_ask"),
            volume=m.get("volume"),
            liquidity=m.get("liquidity"),
        ))
    return out


def query_live_markets(event_id: str, kalshi_series: str = "KXFEDMENTION") -> list[dict]:
    """Page through Kalshi /markets?series_ticker=X&status=open.

    Kalshi's /markets endpoint is PUBLIC — direct requests, no auth.
    """
    import time
    import requests

    BASE = "https://api.elections.kalshi.com/trade-api/v2"
    all_markets: list[dict] = []
    cursor = None
    attempts = 0
    while True:
        params = {"series_ticker": kalshi_series, "status": "open", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{BASE}/markets", params=params, timeout=10)
            if r.status_code == 429:
                attempts += 1
                if attempts < 5:
                    _log.warning(f"rate limited, backing off 5s...")
                    time.sleep(5)
                    continue
                break
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            _log.error(f"query failed: {e}")
            break
        batch = d.get("markets", []) or []
        all_markets.extend(batch)
        cursor = d.get("cursor", "") or ""
        if not cursor or len(batch) < 100:
            break
    return all_markets
