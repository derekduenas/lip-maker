"""Weather Alpha Pillar — directional edge on Kalshi temperature markets.

DATA: api.weather.gov NWS point forecasts (free, no key, REST/JSON).
EDGE: NWS uses NBM ensemble (31 members) → strictly more accurate than the
single-deterministic forecasts casual traders eyeball on weather.com.
Per agent research: 24h MAE ≈ 1.2°F means we can assign meaningful probability
to ADJACENT 2°F brackets while the market over-concentrates on modal.

KALSHI TICKER FORMATS (we parse all variants):
  KXHIGHTNYC-26MAY04-T70   → NYC high temp, 70°F bracket
  KXHIGHTNYC-26MAY04-B68.5 → NYC high temp, bracket boundary at 68.5°F
  KXLOWTHOU-26MAY04-T55    → Houston low temp, 55°F bracket

PIPELINE:
  1. Discover open KXHIGHT*/KXLOWT* markets across all known cities
  2. For each: parse city + target_date + bracket
  3. Pull NWS forecast for that city + date (cached per city/date)
  4. Compute Gaussian probability mass in bracket
  5. Compare to market_yes_price → edge
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from engine.alpha.base import AlphaPillar
from engine.alpha.scrapers.nws import (
    STATION_MAP, fetch_forecast, bracket_probability,
)

_log = logging.getLogger(__name__)

# 2°F bracket assumption matches Kalshi's typical temp market structure.
DEFAULT_BRACKET_WIDTH_F = 2.0
# NWS day-1 MAE is ~1.2°F; we use slightly wider as defensive Gaussian sigma.
FORECAST_SIGMA_F = 1.5

# Series ticker prefix → settlement type
WEATHER_SERIES = {
    # Highs
    "KXHIGHTNYC":  ("NYC", "high"),
    "KXHIGHTLAX":  ("LAX", "high"),
    "KXHIGHTLA":   ("LA",  "high"),
    "KXHIGHTMIA":  ("MIA", "high"),
    "KXHIGHTDEN":  ("DEN", "high"),
    "KXHIGHTAUS":  ("AUS", "high"),
    "KXHIGHTCHI":  ("CHI", "high"),
    "KXHIGHTPHIL": ("PHIL", "high"),
    "KXHIGHTBOS":  ("BOS", "high"),
    "KXHIGHTSFO":  ("SFO", "high"),
    "KXHIGHTSEA":  ("SEA", "high"),
    "KXHIGHTPHX":  ("PHX", "high"),
    "KXHIGHTHOU":  ("HOU", "high"),
    "KXHIGHTDAL":  ("DAL", "high"),
    "KXHIGHTOKC":  ("OKC", "high"),
    "KXHIGHTSAN":  ("SAN", "high"),
    "KXHIGHTATL":  ("ATL", "high"),
    "KXHIGHTLV":   ("LV",  "high"),
    "KXHIGHTMIN":  ("MIN", "high"),
    "KXHIGHTNO":   ("NO",  "high"),
    # Lows
    "KXLOWTNYC":   ("NYC", "low"),
    "KXLOWTLAX":   ("LAX", "low"),
    "KXLOWTLA":    ("LA",  "low"),
    "KXLOWTMIA":   ("MIA", "low"),
    "KXLOWTDEN":   ("DEN", "low"),
    "KXLOWTAUS":   ("AUS", "low"),
    "KXLOWTCHI":   ("CHI", "low"),
    "KXLOWTPHIL":  ("PHIL", "low"),
    "KXLOWTBOS":   ("BOS", "low"),
    "KXLOWTSFO":   ("SFO", "low"),
    "KXLOWTSEA":   ("SEA", "low"),
    "KXLOWTPHX":   ("PHX", "low"),
    "KXLOWTHOU":   ("HOU", "low"),
    "KXLOWTDAL":   ("DAL", "low"),
}


_TICKER_RE = re.compile(
    r"^(?P<series>KX(?:HIGH|LOW)T[A-Z]+)-(?P<dt>\d{2}[A-Z]{3}\d{2})(?:\d{2})?-"
    r"(?P<edge>[BT])(?P<temp>\d+(?:\.\d+)?)$"
)
_DATE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")
_MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def parse_weather_ticker(ticker: str) -> dict | None:
    """Extract (city, kind, target_date, bracket_low, bracket_high) from ticker.
    Returns None if ticker doesn't match known weather format."""
    m = _TICKER_RE.match(ticker)
    if not m:
        return None
    series = m.group("series")
    info = WEATHER_SERIES.get(series)
    if not info:
        return None
    city, kind = info
    # Date parse
    dm = _DATE_RE.match(m.group("dt"))
    if not dm:
        return None
    yy, mmm, dd = dm.groups()
    month = _MONTHS.get(mmm.upper())
    if not month:
        return None
    try:
        target_date = f"20{yy}-{month:02d}-{int(dd):02d}"
    except ValueError:
        return None
    # Bracket parse
    edge = m.group("edge")  # 'B' = boundary, 'T' = bracket-center-ish
    temp = float(m.group("temp"))
    # Kalshi convention varies; treat 'T' as 2°F bracket centered on temp,
    # and 'B' as upper boundary of a 2°F bracket.
    if edge == "T":
        b_low, b_high = temp - 1.0, temp + 1.0
    else:
        b_low, b_high = temp - 2.0, temp
    return {
        "series":      series,
        "city":        city,
        "kind":        kind,
        "target_date": target_date,
        "bracket_low": b_low,
        "bracket_high": b_high,
    }


class WeatherAlpha(AlphaPillar):
    name = "weather"
    min_edge_pct = 0.05
    max_spread_cents = 30
    min_volume_24h = 5
    min_confidence_to_persist = "medium"

    def __init__(self, **kw):
        super().__init__(**kw)
        # Per-(city, date) forecast cache
        self._forecast_cache: dict[tuple[str, str], object] = {}

    def fetch_external_data(self) -> dict:
        # Forecasts fetched lazily per market — cached across markets in
        # same scan. Eager fetch for the 5-7 most active cities.
        self._forecast_cache.clear()
        return {"cache": self._forecast_cache}

    def _get_forecast(self, city: str, target_date: str):
        key = (city.upper(), target_date)
        if key in self._forecast_cache:
            return self._forecast_cache[key]
        try:
            f = fetch_forecast(city, target_date)
        except Exception as e:
            _log.debug(f"forecast fetch failed {city}/{target_date}: {e}")
            f = None
        self._forecast_cache[key] = f
        return f

    def discover_markets(self) -> list[dict]:
        from execution.kalshi_auth import KalshiClient
        c = KalshiClient()
        markets: list[dict] = []
        for series_ticker in WEATHER_SERIES:
            try:
                cursor = None
                while True:
                    params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    r = c.get_unauth("/markets", params=params)
                    batch = r.get("markets", []) or []
                    for m in batch:
                        markets.append({
                            "ticker":         m.get("ticker", ""),
                            "series_ticker":  series_ticker,
                            "title":          m.get("title", ""),
                            "yes_bid":        float(m.get("yes_bid", 0)),
                            "yes_ask":        float(m.get("yes_ask", 100)),
                            "yes_price":      (float(m.get("yes_bid", 0)) +
                                                float(m.get("yes_ask", 100))) / 2,
                            "volume":         m.get("volume", 0),
                            "close_time":     m.get("close_time", ""),
                        })
                    cursor = r.get("cursor")
                    if not cursor:
                        break
            except Exception as e:
                _log.debug(f"discover {series_ticker} failed: {e}")
        return markets

    def compute_fair_prob(self, market: dict, external: dict) -> tuple[float, str]:
        ticker = market["ticker"]
        parsed = parse_weather_ticker(ticker)
        if not parsed:
            return (0.5, "low")
        forecast = self._get_forecast(parsed["city"], parsed["target_date"])
        if not forecast:
            return (0.5, "low")
        # Use high or low based on market kind
        if parsed["kind"] == "high":
            forecast_temp = forecast.high_temp_f
        else:
            forecast_temp = forecast.low_temp_f
        if forecast_temp is None:
            return (0.5, "low")
        prob = bracket_probability(
            forecast_temp, FORECAST_SIGMA_F,
            parsed["bracket_low"], parsed["bracket_high"],
        )
        # Confidence: same-day = high, +1d = medium, +2d+ = low
        try:
            target = datetime.strptime(parsed["target_date"], "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            days_ahead = (target - today).days
        except Exception:
            days_ahead = 99
        if days_ahead <= 0:
            confidence = "high"
        elif days_ahead == 1:
            confidence = "medium"
        else:
            confidence = "low"
        return (max(0.01, min(0.99, prob)), confidence)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--bankroll", type=float, default=200.0)
    p.add_argument("--persist", action="store_true")
    a = p.parse_args()
    pillar = WeatherAlpha(bankroll_usd=a.bankroll)
    edges = pillar.find_edges()
    print(f"\n=== WEATHER ALPHA — {len(edges)} edges found ===\n")
    print(f"{'TICKER':<35} {'TITLE':<40} {'FAIR%':>6} {'MKT%':>6} {'EDGE':>7} {'CONF':>6} {'$REC':>6}")
    print("-" * 115)
    for e in edges[:25]:
        print(f"{e.market_ticker[:33]:<35} {e.features.get('market', '')[:38]:<40} "
              f"{e.fair_prob*100:>5.1f}% {e.market_yes_price*100:>5.1f}% "
              f"{e.edge_pct*100:>+6.2f}% {e.confidence:>6} ${e.recommended_usd:>5.2f}")
    if a.persist and edges:
        n = pillar.persist_predictions(edges)
        print(f"\n→ persisted {n} edges to alpha_predictions table")
