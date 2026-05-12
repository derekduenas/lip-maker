"""WeatherBrain — predict daily HIGH/LOW temperature outcomes vs Kalshi
threshold markets, using free NWS forecasts.

Market structure (verified 2026-05-12 against live Kalshi markets):
    Series prefixes: KXHIGHT{CITY}, KXLOWT{CITY}, KXHIGH{CITY}, KXLOW{CITY}
        (mix of T-suffixed and not — Kalshi inconsistency)
    Ticker:    KXLOWTLAX-26MAY12-T60
    Title:     "Will the minimum temperature be >60° on May 12, 2026?"
    yes_sub:   "61° or above"   (canonical direction)
                or "52° or below"
    Rules:     "If the minimum temperature recorded at Los Angeles for
                May 12, 2026, is greater than 60° fahrenheit according to
                the National Weather Service's Climatological Report
                (Daily), then the market resolves to Yes."
    Settles:   close_time ~ 08:00 UTC the day AFTER the measurement date

Why NWS forecast → edge:
    - Free, accurate (~±2°F at 1-day lead, ±5°F at 7-day lead)
    - Same data source Kalshi uses to settle (own forecasts feed market
      makers, but settlement is observation, so forecast→observation
      uncertainty is what creates edge)
    - Daily settlement → fast feedback loop (track record builds in
      days, not weeks like the monthly Music brain)

Probability model:
    p_yes = P(actual_temp [> | <] threshold) under N(forecast, σ_lead)
    σ_lead is calibrated to NWS error bands by forecast horizon.

Confidence:
    extremes (very far from threshold, short lead) → high
    p ≈ 0.5 (close call, long lead) → low
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

from argus.brains.base import DomainBrain, Prediction, confidence_from_extremes
from argus.data.nws import (
    NWSClient, KALSHI_CITY_TO_NWS, climatology_normal_high_low,
)

_log = logging.getLogger(__name__)


# Series prefixes we claim. Both "T" and non-"T" variants exist on Kalshi.
HIGH_PREFIXES = (
    "KXHIGHT",   # "T" = temperature explicit
    "KXHIGH",    # legacy / inconsistent (KXHIGHNY, KXHIGHLAX, KXHIGHMIA)
)
LOW_PREFIXES = (
    "KXLOWT",
    "KXLOW",
)
ALL_PREFIXES = HIGH_PREFIXES + LOW_PREFIXES

# Filter: drop NON-temperature ones whose tickers happen to start with
# KXHIGH/KXLOW (KXHIGHINFLATION, KXHIGHMOVDJT, KXLOWESTRATE, ...).
_NON_TEMP_KEYWORDS_RX = re.compile(
    r"INFLATION|RATE|MOV|MOVDJT|MOVKH|YIELD|VIX|UMICH|DXY",
    re.IGNORECASE,
)


# NWS forecast error stdev (°F) by lead time in days. Pulled from NOAA
# verification stats — typical maximum-temperature MAE for surface
# stations. Conservative (slightly higher than published) to keep the
# probability model honest about its uncertainty.
ERROR_STDEV_BY_LEAD_DAYS = {
    0: 1.5,   # same-day forecast (morning-of)
    1: 2.5,
    2: 3.0,
    3: 3.5,
    4: 4.0,
    5: 4.5,
    6: 5.0,
    7: 5.5,
}
DEFAULT_STDEV_BEYOND = 6.5


def _stdev_for_lead(lead_days: int) -> float:
    if lead_days < 0:
        return 1.0           # market already past — should not happen
    if lead_days <= 7:
        return ERROR_STDEV_BY_LEAD_DAYS[lead_days]
    return DEFAULT_STDEV_BEYOND


# ── Ticker + market parsing ───────────────────────────────────────────────
_RX_TICKER = re.compile(
    r"^(?P<prefix>KX(?:HIGHT|HIGH|LOWT|LOW))"
    r"(?P<city>[A-Z]+)-"
    r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})-"
    r"T(?P<strike>\d+(?:\.\d+)?)$"
)
MONTH_ABBR = {
    "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
    "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12,
}

# yes_sub_title canonical patterns. Direction "above" = YES when actual >=
# the strike value in the title (typically strike + 1); "below" = YES when
# actual <= the strike value (typically strike - 1).
_RX_YES_SUB = re.compile(
    r"(?P<value>-?\d+(?:\.\d+)?)\s*°?\s*or\s+(?P<dir>above|below)",
    re.IGNORECASE,
)


@dataclass
class WeatherMeta:
    ticker:        str
    series_prefix: str        # e.g. "KXLOWT"
    city_token:    str        # e.g. "LAX"
    settle_date:   dt.date    # the measurement date
    strike_temp:   float      # threshold from ticker
    yes_value:     float      # canonical YES boundary from yes_sub_title
    direction:     str        # "above" or "below"
    is_high:       bool       # high-temp market vs low-temp
    nws_station:   str
    chart_region:  str        # for stats parity with MusicBrain — "WEATHER"


def parse_market(market: dict) -> Optional[WeatherMeta]:
    ticker = market.get("ticker", "")
    m = _RX_TICKER.match(ticker)
    if not m:
        return None
    prefix    = m.group("prefix")
    city      = m.group("city")
    is_high   = prefix.startswith("KXHIGH")
    strike    = float(m.group("strike"))

    # Reject non-temperature siblings (KXHIGHINFLATION etc.)
    if _NON_TEMP_KEYWORDS_RX.search(city):
        return None

    # Date
    mon = MONTH_ABBR.get(m.group("mon"))
    if not mon:
        return None
    try:
        settle_date = dt.date(2000 + int(m.group("yy")), mon, int(m.group("dd")))
    except ValueError:
        return None

    # Direction + canonical YES boundary from yes_sub_title
    sub = market.get("yes_sub_title", "") or ""
    sm = _RX_YES_SUB.search(sub)
    if not sm:
        return None
    yes_value = float(sm.group("value"))
    direction = sm.group("dir").lower()

    info = KALSHI_CITY_TO_NWS.get(city.upper())
    if info is None:
        return None
    station = info[0]

    return WeatherMeta(
        ticker=ticker, series_prefix=prefix, city_token=city,
        settle_date=settle_date, strike_temp=strike,
        yes_value=yes_value, direction=direction, is_high=is_high,
        nws_station=station, chart_region="WEATHER",
    )


# ── Probability core ──────────────────────────────────────────────────────
def _normal_cdf(x: float) -> float:
    """Standard normal CDF via erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def p_yes_for(direction: str, *, forecast_temp: float,
              yes_value: float, sigma: float) -> float:
    """Compute P(actual matches YES condition).

    The yes_value is the canonical YES boundary from yes_sub_title:
      "61° or above" → direction='above', yes_value=61, YES iff actual ≥ 61
      "52° or below" → direction='below', yes_value=52, YES iff actual ≤ 52

    Treat actual ~ N(forecast_temp, sigma).
      P(actual ≥ y) = 1 - Φ((y - 0.5 - forecast) / sigma)   [continuity correction]
      P(actual ≤ y) = Φ((y + 0.5 - forecast) / sigma)
    """
    if sigma <= 0:
        sigma = 0.5
    if direction == "above":
        z = (yes_value - 0.5 - forecast_temp) / sigma
        return max(0.001, min(0.999, 1.0 - _normal_cdf(z)))
    elif direction == "below":
        z = (yes_value + 0.5 - forecast_temp) / sigma
        return max(0.001, min(0.999, _normal_cdf(z)))
    return 0.5


# ── Brain ─────────────────────────────────────────────────────────────────
class WeatherBrain(DomainBrain):
    brain_id       = "weather"
    market_pattern = "KX(HIGH|LOW)T?*-*"

    def __init__(self, *, client: Optional[NWSClient] = None) -> None:
        self.client = client or NWSClient()

    def can_evaluate(self, market_ticker: str) -> bool:
        return any(market_ticker.startswith(p) for p in ALL_PREFIXES)

    def predict(self, market: dict) -> Optional[Prediction]:
        meta = parse_market(market)
        if meta is None:
            return None

        today = dt.date.today()
        lead = (meta.settle_date - today).days
        if lead < 0:
            # Market already settled (we shouldn't see these from active scan)
            return None

        sigma = _stdev_for_lead(lead)

        # Try NWS forecast first
        forecast_temp = None
        forecast_source = "nws"
        try:
            sf = self.client.get_forecast(meta.city_token)
        except Exception as e:
            _log.warning(f"forecast fetch failed for {meta.city_token}: {e}")
            sf = None
        if sf is not None:
            high, low = sf.daily_high_low(meta.settle_date)
            forecast_temp = high if meta.is_high else low

        # Fall back to climatology when forecast unavailable for that date
        if forecast_temp is None:
            cm = climatology_normal_high_low(meta.nws_station, meta.settle_date.month)
            if cm is None:
                return None
            forecast_temp = cm[0] if meta.is_high else cm[1]
            forecast_source = "climatology"
            # Climatology has much wider uncertainty than a forecast
            sigma = max(sigma, 7.0)

        p = p_yes_for(meta.direction, forecast_temp=float(forecast_temp),
                      yes_value=meta.yes_value, sigma=sigma)

        return Prediction(
            p_yes=p,
            confidence=confidence_from_extremes(p),
            key_features={
                "city":              meta.city_token,
                "nws_station":       meta.nws_station,
                "settle_date":       meta.settle_date.isoformat(),
                "is_high":           meta.is_high,
                "direction":         meta.direction,
                "yes_value":         meta.yes_value,
                "strike_temp":       meta.strike_temp,
                "forecast_temp":     float(forecast_temp),
                "forecast_source":   forecast_source,
                "lead_days":         lead,
                "sigma_used":        sigma,
                "chart_region":      meta.chart_region,
            },
            brain_id=self.brain_id,
            market_ticker=meta.ticker,
        )


# ── Self-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Synthetic markets exercising the parser + math
    samples = [
        {"ticker": "KXLOWTLAX-26MAY12-T60",
         "yes_sub_title": "61° or above"},
        {"ticker": "KXLOWTLAX-26MAY12-T53",
         "yes_sub_title": "52° or below"},
        {"ticker": "KXHIGHTPHX-26MAY15-T100",
         "yes_sub_title": "101° or above"},
        {"ticker": "KXHIGHINFLATION-26MAY12-T3",
         "yes_sub_title": "3% or above"},     # should be filtered
    ]
    print("=== Parse synthetic markets ===")
    for s in samples:
        meta = parse_market(s)
        if meta is None:
            print(f"  {s['ticker']:<35}  -> SKIPPED")
        else:
            print(f"  {s['ticker']:<35}  -> {meta.city_token} "
                  f"{meta.settle_date} {'HIGH' if meta.is_high else 'LOW'} "
                  f"{meta.direction} {meta.yes_value}°  station={meta.nws_station}")

    # Probability spot-check
    print("\n=== Probability spot-check ===")
    # Forecast 65, threshold "60° or above", sigma 2.5
    # Should be very high probability (forecast well above threshold)
    p = p_yes_for("above", forecast_temp=65, yes_value=60, sigma=2.5)
    print(f"  forecast 65, '>=60', σ=2.5  -> p={p:.3f}  (expect >0.95)")
    p = p_yes_for("above", forecast_temp=58, yes_value=60, sigma=2.5)
    print(f"  forecast 58, '>=60', σ=2.5  -> p={p:.3f}  (expect <0.30)")
    p = p_yes_for("below", forecast_temp=58, yes_value=60, sigma=2.5)
    print(f"  forecast 58, '<=60', σ=2.5  -> p={p:.3f}  (expect >0.70)")
    p = p_yes_for("above", forecast_temp=60, yes_value=60, sigma=2.5)
    print(f"  forecast 60, '>=60', σ=2.5  -> p={p:.3f}  (expect ~0.5)")

    # Live spot-check: NYC tomorrow's high vs synthetic threshold
    print("\n=== Live: NYC tomorrow forecast ===")
    brain = WeatherBrain()
    tmrw = dt.date.today() + dt.timedelta(days=1)
    test_market = {
        "ticker": f"KXHIGHTNY-{tmrw.strftime('%y%b%d').upper()}-T70",
        "yes_sub_title": "71° or above",
    }
    pred = brain.predict(test_market)
    if pred is None:
        print("  no prediction (parse miss or station unknown)")
    else:
        print(f"  ticker:    {pred.market_ticker}")
        print(f"  p_yes:     {pred.p_yes:.4f}")
        print(f"  conf:      {pred.confidence:.4f}")
        for k, v in pred.key_features.items():
            print(f"    {k:<20} = {v}")
    print("\nOK weather")
