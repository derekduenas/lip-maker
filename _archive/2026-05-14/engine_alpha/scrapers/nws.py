"""National Weather Service forecast scraper — api.weather.gov.

WHY: Kalshi temperature markets (KXHIGHT*, KXLOWT*) settle on the NWS Daily
Climate Report from specific ASOS stations (KNYC=Central Park, KLAX, KMDW,
etc.). Casual traders eyeball weather.com (single deterministic forecast).
We pull NWS's own point forecasts which are based on the National Blend of
Models (NBM) — a 31-member ensemble that's strictly more accurate than any
single model.

EDGE: Per agent research (2026-05-03), 24h forecasts have ~1.2°F MAE for
high temps. That means our distribution gives meaningful probability mass
on adjacent 2°F brackets, while market often over-concentrates on modal.
Documented 4-8pp fee-adjusted edges on adjacent brackets.

SETTLEMENT MAPPING (Kalshi → NWS station):
  KXHIGHT/LOWT NYC  → KNYC (Central Park)
  KXHIGHT/LOWT LAX  → KLAX
  KXHIGHT/LOWT MIA  → KMIA
  KXHIGHT/LOWT DEN  → KDEN
  KXHIGHT/LOWT AUS  → KAUS
  KXHIGHT/LOWT CHI  → KMDW (Midway, NOT ORD)
  KXHIGHT/LOWT PHIL → KPHL
  KXHIGHT/LOWT MIA  → KMIA
  KXHIGHT/LOWT BOS  → KBOS
  KXHIGHT/LOWT SFO  → KSFO
  KXHIGHT/LOWT SEA  → KSEA
  KXHIGHT/LOWT PHX  → KPHX
  KXHIGHT/LOWT HOU  → KIAH (Houston Intercontinental)

API: api.weather.gov is free, no auth required. Be polite (5s delay).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

USER_AGENT = "INNAIT-research-bot (contact: trader@innait)"
TIMEOUT = 30
POLITENESS_SEC = 3

_log = logging.getLogger(__name__)


# Kalshi city code → NWS station + lat/lon (for grid-point lookups)
STATION_MAP = {
    "NYC":  {"icao": "KNYC", "lat": 40.78, "lon": -73.97, "tz_offset": -5},
    "LAX":  {"icao": "KLAX", "lat": 33.94, "lon": -118.41, "tz_offset": -8},
    "LA":   {"icao": "KLAX", "lat": 33.94, "lon": -118.41, "tz_offset": -8},
    "MIA":  {"icao": "KMIA", "lat": 25.79, "lon": -80.32, "tz_offset": -5},
    "DEN":  {"icao": "KDEN", "lat": 39.86, "lon": -104.67, "tz_offset": -7},
    "AUS":  {"icao": "KAUS", "lat": 30.18, "lon": -97.68, "tz_offset": -6},
    "CHI":  {"icao": "KMDW", "lat": 41.79, "lon": -87.75, "tz_offset": -6},
    "PHIL": {"icao": "KPHL", "lat": 39.87, "lon": -75.24, "tz_offset": -5},
    "BOS":  {"icao": "KBOS", "lat": 42.36, "lon": -71.01, "tz_offset": -5},
    "SFO":  {"icao": "KSFO", "lat": 37.62, "lon": -122.38, "tz_offset": -8},
    "SEA":  {"icao": "KSEA", "lat": 47.45, "lon": -122.31, "tz_offset": -8},
    "PHX":  {"icao": "KPHX", "lat": 33.43, "lon": -112.01, "tz_offset": -7},
    "HOU":  {"icao": "KIAH", "lat": 29.98, "lon": -95.34, "tz_offset": -6},
    "DAL":  {"icao": "KDFW", "lat": 32.90, "lon": -97.04, "tz_offset": -6},
    "OKC":  {"icao": "KOKC", "lat": 35.39, "lon": -97.60, "tz_offset": -6},
    "SAN":  {"icao": "KSAT", "lat": 29.53, "lon": -98.47, "tz_offset": -6},
    "ATL":  {"icao": "KATL", "lat": 33.64, "lon": -84.43, "tz_offset": -5},
    "LV":   {"icao": "KLAS", "lat": 36.08, "lon": -115.15, "tz_offset": -8},
    "VEG":  {"icao": "KLAS", "lat": 36.08, "lon": -115.15, "tz_offset": -8},
    "MIN":  {"icao": "KMSP", "lat": 44.88, "lon": -93.22, "tz_offset": -6},
    "NO":   {"icao": "KMSY", "lat": 29.99, "lon": -90.26, "tz_offset": -6},
}


@dataclass
class WeatherForecast:
    """Per-day forecast for a city."""
    city_code:     str
    icao:          str
    target_date:   str        # YYYY-MM-DD (local)
    high_temp_f:   float | None
    low_temp_f:    float | None
    high_uncertainty_f: float = 1.5  # NWS day-1 typical MAE
    low_uncertainty_f:  float = 1.5
    fetched_at:    str = ""


def _get_grid_point(lat: float, lon: float) -> tuple[str, int, int] | None:
    """Convert lat/lon → NWS grid point (office, gridX, gridY).
    Required for the /points/ endpoint. Cached per city."""
    try:
        time.sleep(POLITENESS_SEC)
        r = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        props = r.json().get("properties", {})
        office = props.get("gridId")
        gx = props.get("gridX")
        gy = props.get("gridY")
        if office and gx is not None and gy is not None:
            return office, gx, gy
    except Exception as e:
        _log.warning(f"NWS points lookup failed for {lat},{lon}: {e}")
    return None


_grid_cache: dict[str, tuple[str, int, int]] = {}


def _cached_grid(city_code: str) -> tuple[str, int, int] | None:
    if city_code in _grid_cache:
        return _grid_cache[city_code]
    info = STATION_MAP.get(city_code)
    if not info:
        return None
    grid = _get_grid_point(info["lat"], info["lon"])
    if grid:
        _grid_cache[city_code] = grid
    return grid


def fetch_forecast(city_code: str, target_date: str = None) -> WeatherForecast | None:
    """Pull NWS hourly forecast and extract high/low for target_date.

    target_date: YYYY-MM-DD (local). Defaults to TODAY if not given.
    Returns WeatherForecast or None if unavailable."""
    info = STATION_MAP.get(city_code.upper())
    if not info:
        _log.debug(f"unknown city code: {city_code}")
        return None
    grid = _cached_grid(city_code.upper())
    if not grid:
        return None
    office, gx, gy = grid
    if not target_date:
        target_date = datetime.now(timezone.utc).date().isoformat()
    # Pull hourly forecast (richer than daily — gives us per-hour temps)
    try:
        time.sleep(POLITENESS_SEC)
        r = requests.get(
            f"https://api.weather.gov/gridpoints/{office}/{gx},{gy}/forecast/hourly",
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        periods = r.json().get("properties", {}).get("periods", []) or []
    except Exception as e:
        _log.warning(f"NWS hourly forecast failed for {city_code}: {e}")
        return None

    # Filter to periods on target_date (local time approximated via tz_offset)
    tz_offset = info["tz_offset"]
    high = low = None
    for period in periods:
        start_iso = period.get("startTime", "")
        if not start_iso:
            continue
        try:
            dt = datetime.fromisoformat(start_iso)
            local_date = (dt.astimezone(timezone.utc).replace(tzinfo=None)).date()
            # Approximate local date by adding tz_offset hours
            from datetime import timedelta
            local_date = (dt + timedelta(hours=tz_offset)).date().isoformat()
        except Exception:
            continue
        if local_date != target_date:
            continue
        temp_f = period.get("temperature")
        if temp_f is None:
            continue
        if high is None or temp_f > high:
            high = float(temp_f)
        if low is None or temp_f < low:
            low = float(temp_f)
    if high is None and low is None:
        return None
    return WeatherForecast(
        city_code=city_code.upper(),
        icao=info["icao"],
        target_date=target_date,
        high_temp_f=high,
        low_temp_f=low,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def bracket_probability(forecast_temp: float, uncertainty_f: float,
                        bracket_low: float, bracket_high: float) -> float:
    """Probability that observed temp falls in [bracket_low, bracket_high]
    given forecast = (forecast_temp ± uncertainty_f). Uses Gaussian approx.

    Returns prob in [0, 1].
    """
    import math
    if forecast_temp is None:
        return 0.0
    sigma = max(0.5, uncertainty_f)
    # Gaussian CDF approximation
    def _phi(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    z_low = (bracket_low - forecast_temp) / sigma
    z_high = (bracket_high - forecast_temp) / sigma
    return max(0.0, min(1.0, _phi(z_high) - _phi(z_low)))


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--city", default="NYC")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default today)")
    a = p.parse_args()
    f = fetch_forecast(a.city, a.date)
    if f:
        print(f"\n=== NWS Forecast — {f.city_code} ({f.icao}) for {f.target_date} ===")
        print(f"  High: {f.high_temp_f}°F  ± {f.high_uncertainty_f}°F")
        print(f"  Low:  {f.low_temp_f}°F  ± {f.low_uncertainty_f}°F")
        print(f"\nExample bracket probabilities for HIGH:")
        if f.high_temp_f:
            for low_b in range(int(f.high_temp_f) - 4, int(f.high_temp_f) + 5, 2):
                prob = bracket_probability(f.high_temp_f, 1.5, low_b, low_b + 2)
                bar = "█" * int(prob * 30)
                print(f"  {low_b}–{low_b+2}°F: {prob*100:>5.1f}%  {bar}")
    else:
        print(f"No forecast for {a.city}")
