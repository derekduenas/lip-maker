"""Per-series Kalshi fee resolution (2026-09-30).

engine/fees.py applies ONE schedule to every market: the 0.07 taker-style
rate charged on maker fills too. Kalshi does not work that way. Each series
publishes `fee_type` and `fee_multiplier` on GET /trade-api/v2/series/{t}
(events may override via fee_type_override / fee_multiplier_override), and
the help centre says maker fees apply only "in some cases"
(help.kalshi.com/en/articles/13823805-fees). A public pull on 2026-09-30
(research/lip_data/kalshi_all_series.json) found:

    quadratic                         14,355 series  — no maker fee
    quadratic_with_maker_fees            160 series
    quadratic_with_combo_maker_fees        3 series

So for ~99% of series the global schedule charges a maker fee that does
not exist (1.75c/contract at 50c), which makes the economic layer refuse
markets for the wrong reason.

Rates: taker 0.07 × M and maker 0.0175 × M come from third-party fee
trackers (the official PDF sits behind a bot-check from this box), so they
stay `verified=False`. The *fee_type* split is from the venue API and is the
part this module relies on. Rounding: ceil to $0.000001 per fill (verified,
docs.kalshi.com/getting_started/fee_rounding).

Default OFF (settings.SERIES_FEES_ENABLED) because it makes paper economics
less conservative; flip it in paper first and compare.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from engine.fees import FeeSchedule

_log = logging.getLogger(__name__)

TAKER_BASE = Decimal("0.07")
# Fee schedule PDF, July 2026 (7.7.26), https://kalshi.com/docs/kalshi-fee-schedule.pdf
# maker = M × 0.0175 × C × P × (1−P) where maker fees apply.
# Series schema (docs.kalshi.com Get Series, fetched 2026-10-01): combo maker
# multiplier is 0.5 of the taker coefficient, standard maker is 0.25.
# 0.07 × 0.25 = 0.0175; 0.07 × 0.50 = 0.035.
MAKER_BASE = Decimal("0.0175")
MAKER_COMBO_BASE = Decimal("0.035")
MAKER_FEE_TYPES = frozenset({"quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"})
KNOWN_FEE_TYPES = MAKER_FEE_TYPES | {"quadratic"}


@dataclass(frozen=True)
class SeriesFeeSchedule:
    """Duck-type compatible with engine.fees.FeeSchedule for callers that use
    fee_usd / round_trip_usd / describe / verified / name / source."""
    series: str
    fee_type: str
    multiplier: Decimal
    verified: bool = False

    @property
    def name(self) -> str:
        return f"kalshi_series[{self.series}:{self.fee_type}x{self.multiplier}]"

    @property
    def source(self) -> str:
        return ("fee_type/fee_multiplier from Kalshi /series API; base rates "
                "0.07 taker / 0.0175 maker from trackers (unverified)")

    @property
    def maker_charged(self) -> bool:
        return self.fee_type in MAKER_FEE_TYPES

    def _maker_base(self) -> Decimal:
        if self.fee_type == "quadratic_with_combo_maker_fees":
            return MAKER_COMBO_BASE
        if self.fee_type == "quadratic_with_maker_fees":
            return MAKER_BASE
        return Decimal("0")

    def _schedule(self, is_taker: bool) -> FeeSchedule:
        rate = (TAKER_BASE if is_taker else self._maker_base()) * self.multiplier
        return FeeSchedule(name=self.name, rate=rate, source=self.source,
                           verified=False, rounding="ceil_6dp",
                           charge_maker=self.maker_charged)

    def fee_usd(self, price_cents, contracts, *, is_taker: bool = False) -> Decimal:
        return self._schedule(is_taker).fee_usd(price_cents, contracts, is_taker=is_taker)

    def round_trip_usd(self, entry_cents, exit_cents, contracts) -> Decimal:
        return (self.fee_usd(entry_cents, contracts, is_taker=False)
                + self.fee_usd(exit_cents, contracts, is_taker=True))

    def describe(self) -> dict:
        return {"name": self.name, "series": self.series, "fee_type": self.fee_type,
                "multiplier": str(self.multiplier), "maker_charged": self.maker_charged,
                "verified": self.verified, "source": self.source}


def schedule_from_series(series_json: dict) -> Optional[SeriesFeeSchedule]:
    """Build from a /series/{ticker} payload ({"series": {...}} or the inner dict).
    Returns None for unknown fee types — callers fall back to the global
    conservative schedule rather than guess."""
    s = series_json.get("series", series_json) if isinstance(series_json, dict) else {}
    ft = s.get("fee_type")
    if ft not in KNOWN_FEE_TYPES:
        return None
    try:
        m = Decimal(str(s.get("fee_multiplier", 1)))
    except Exception:
        return None
    if m < 0:
        return None
    return SeriesFeeSchedule(series=str(s.get("ticker", "")), fee_type=ft, multiplier=m)


class SeriesFeeResolver:
    """Cache of per-series schedules with an injectable fetcher
    (ticker -> /series payload). Failures fall back to `fallback`."""

    def __init__(self, fetcher: Callable[[str], dict], fallback):
        self._fetch = fetcher
        self._fallback = fallback
        self._cache: dict[str, object] = {}

    def for_ticker(self, market_ticker: str):
        series = (market_ticker or "").split("-", 1)[0]
        if not series:
            return self._fallback
        if series not in self._cache:
            sched = None
            try:
                sched = schedule_from_series(self._fetch(series))
            except Exception as e:
                _log.info(f"series fee fetch failed for {series}: {e}")
            self._cache[series] = sched
        return self._cache[series] or self._fallback


def http_series_fetcher(base_url: str = "https://api.elections.kalshi.com/trade-api/v2",
                        timeout: float = 5.0) -> Callable[[str], dict]:
    """Public, unauthenticated, read-only metadata fetch."""
    import requests

    def _f(series: str) -> dict:
        r = requests.get(f"{base_url}/series/{series}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    return _f
