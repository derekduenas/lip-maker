"""Market close time, from the venue — kept distinct from reward expiry.

Two different clocks
--------------------
A LIP program has a reward window (`start_date`/`end_date` on
/incentive_programs). The contract has a close/settlement time
(`close_time` on /markets/{ticker}). They are NOT the same clock, and
measurement on live data shows them diverging by a week:

    KXTTELITEMATCH-26SEP210030ASOMLU-ASO
        incentive ends  in    86 minutes
        market closes   in 10,106 minutes  (7 days)

Conflating them is a real error in both directions. Treating reward expiry
as settlement makes us flatten inventory in a market that will trade for
another week. Treating settlement as reward expiry keeps us quoting for a
reward that stopped accruing.

So: reward expiry stops reward-driven ENTRY. Only market close is allowed
to drive settlement-risk behaviour. Inventory management answers to
neither — a position is managed until it is flat.

Why this module replaces ticker parsing
---------------------------------------
`run_paper._minutes_until_settle` derived close time by regex over the
ticker string. Measured against the API on live markets it is wrong often
enough to be dangerous:

    KXTTELITEMATCH-26SEP210030ASOMLU-ASO   parsed -4.1m   actual 10,106m
    KXTRUMPPHOTO-26SEP27-7                 parsed 9,835m  actual 10,676m
    KXCRYPTOLEAD15M-26SEP210015-HYPE       parsed None    actual 10.9m

The last is the worst: `None` meant the pre-settlement gate silently did
not fire AND the economics fell back to assuming 24 hours to settle, for a
market closing in eleven minutes.

This module asks the venue, caches the answer, and when it genuinely does
not know it says UNKNOWN rather than returning a number someone will trust.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger(__name__)

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CACHE_TTL_SEC = 300.0

UNKNOWN = "unknown"
SOURCE_API = "api_close_time"
SOURCE_TICKER = "ticker_parse_fallback"


def _ctx() -> ssl.SSLContext:
    try:
        if ssl.get_default_verify_paths().cafile:
            return ssl.create_default_context()
    except Exception:
        pass
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


@dataclass(frozen=True)
class CloseTime:
    """When the contract closes, and how confident we are."""
    ticker: str
    close_ts: Optional[float]
    source: str
    open_ts: Optional[float] = None

    @property
    def duration_min(self) -> Optional[float]:
        """The market's own trading window, in minutes. None when unknown —
        the proportional cutoff policy falls back to the control rather than
        inventing a denominator."""
        if self.close_ts is None or self.open_ts is None:
            return None
        d = (self.close_ts - self.open_ts) / 60.0
        return d if d > 0 else None

    @property
    def known(self) -> bool:
        return self.close_ts is not None and self.source != UNKNOWN

    def minutes_until(self, now: Optional[float] = None) -> Optional[float]:
        if not self.known:
            return None
        return (self.close_ts - (time.time() if now is None else now)) / 60.0


class MarketClock:
    """Caches per-market close times fetched from the venue."""

    def __init__(self, *, ttl_sec: float = CACHE_TTL_SEC,
                 fetcher=None, allow_ticker_fallback: bool = False):
        self._cache: dict[str, tuple[CloseTime, float]] = {}
        self._ttl = ttl_sec
        self._ctx = _ctx()
        self._fetcher = fetcher
        # OFF by default. The parser is measurably wrong by days on real
        # tickers, so it is opt-in and always labelled in `source`.
        self.allow_ticker_fallback = allow_ticker_fallback
        self.fetches = 0
        self.fetch_errors = 0

    def _fetch(self, ticker: str) -> Optional[str]:
        if self._fetcher is not None:
            return self._fetcher(ticker)
        url = f"{API_BASE}/markets/{ticker}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20, context=self._ctx) as r:
            mk = json.loads(r.read()).get("market") or {}
        return {"close_time": mk.get("close_time"), "open_time": mk.get("open_time")}

    def close_time(self, ticker: str, *, ticker_parse=None) -> CloseTime:
        hit = self._cache.get(ticker)
        now = time.time()
        if hit is not None and now - hit[1] < self._ttl:
            return hit[0]
        ct: Optional[float] = None
        ot: Optional[float] = None
        source = UNKNOWN

        def _p(v):
            return (datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                    .timestamp()) if v else None
        try:
            self.fetches += 1
            raw = self._fetch(ticker)
            if isinstance(raw, dict):
                ct, ot = _p(raw.get("close_time")), _p(raw.get("open_time"))
            else:
                ct = _p(raw)
            if ct is not None:
                source = SOURCE_API
        except Exception as e:
            self.fetch_errors += 1
            _log.debug(f"close_time fetch failed {ticker}: {e}")
        if ct is None and self.allow_ticker_fallback and ticker_parse is not None:
            try:
                mins = ticker_parse(ticker)
                if mins is not None:
                    ct = now + float(mins) * 60.0
                    source = SOURCE_TICKER
            except Exception:
                pass
        out = CloseTime(ticker=ticker, close_ts=ct, source=source, open_ts=ot)
        self._cache[ticker] = (out, now)
        return out

    def minutes_until_close(self, ticker: str, *, ticker_parse=None
                            ) -> Optional[float]:
        """Minutes to close, or None when genuinely unknown.

        None means UNKNOWN. Callers must treat it as unknown — not as
        'far away', which is the mistake the old 24-hour fallback made.
        """
        return self.close_time(ticker, ticker_parse=ticker_parse).minutes_until()

    def summary(self) -> dict:
        known = sum(1 for v, _ in self._cache.values() if v.known)
        return {"cached": len(self._cache), "known": known,
                "unknown": len(self._cache) - known,
                "fetches": self.fetches, "fetch_errors": self.fetch_errors}
