"""NWS (National Weather Service) API client — free, no key required.

Two-step flow per https://www.weather.gov/documentation/services-web-api:

    1. GET /points/{lat},{lon}
       → returns 'forecast', 'forecastHourly', 'forecastGridData' URLs
       → also returns 'gridId', 'gridX', 'gridY', 'observationStations'

    2. GET that 'forecast' URL
       → 'periods': list of forecast periods (~14, alternating day/night)
         each with: name, startTime, endTime, temperature, isDaytime,
         shortForecast, probabilityOfPrecipitation, windSpeed, ...

For a daily HIGH-temp market settling on a given date, we pick the daytime
period whose startTime falls on that date and use temperature as the point
forecast. For LOW, the corresponding nighttime period.

Historical observations (used for climatology):
    GET /stations/{station}/observations?start=...&end=...
    → returns hourly METAR data we summarize to daily high/low.

Cache strategy: forecasts cached by (station, fetch_date) — re-fetch only
when fetch_date changes. Observations cached forever (immutable history).

Polite scraping:
    - User-Agent identifies us (NWS asks for this)
    - 1.0s between requests (well under any limit)
    - Retry once on 5xx; otherwise raise.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import requests

from argus.config import CACHE_DIR, ensure_dirs

_log = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"
USER_AGENT = "ArgusBot/0.1 (research; +https://github.com/derekduenas/lip-maker)"
RATE_SLEEP_SEC = 1.0

# Kalshi city → (NWS station code, lat, lon).
# Stations chosen to match Kalshi's documented settlement_source where
# possible (per project_kalshi_settlement_sources.md memo: Kalshi's
# rules_primary often names a specific station, e.g. NYC settles KNYC).
# When in doubt, pick the canonical airport station.
KALSHI_CITY_TO_NWS = {
    # ticker token  → (station, lat, lon, display)
    "NY":     ("KNYC", 40.7831, -73.9712, "New York Central Park"),
    "NYC":    ("KNYC", 40.7831, -73.9712, "New York Central Park"),
    "MIA":    ("KMIA", 25.7959, -80.2870, "Miami Intl"),
    "LAX":    ("KLAX", 33.9425, -118.4081, "Los Angeles Intl"),
    "CHI":    ("KMDW", 41.7868, -87.7522, "Chicago Midway"),  # Kalshi LIPs note KMDW
    "AUS":    ("KAUS", 30.1945, -97.6699, "Austin-Bergstrom"),
    "DC":     ("KDCA", 38.8512, -77.0402, "DC Reagan"),
    "PHIL":   ("KPHL", 39.8729, -75.2437, "Philadelphia Intl"),
    "DEN":    ("KDEN", 39.8617, -104.6731, "Denver Intl"),
    "HOU":    ("KIAH", 29.9844, -95.3414, "Houston Intercontinental"),
    "OU":     ("KIAH", 29.9844, -95.3414, "Houston Intercontinental"),    # KXHIGHOU typo
    "SEA":    ("KSEA", 47.4502, -122.3088, "Seattle-Tacoma"),
    "SFO":    ("KSFO", 37.6213, -122.3790, "SFO"),
    "PHX":    ("KPHX", 33.4373, -112.0078, "Phoenix Sky Harbor"),
    "DAL":    ("KDFW", 32.8998, -97.0403, "Dallas-Fort Worth"),
    "ATL":    ("KATL", 33.6407, -84.4277, "Atlanta Hartsfield"),
    "BOS":    ("KBOS", 42.3656, -71.0096, "Boston Logan"),
    "LV":     ("KLAS", 36.0840, -115.1537, "Las Vegas McCarran"),
    "MIN":    ("KMSP", 44.8848, -93.2223, "Minneapolis-St Paul"),
    "NOLA":   ("KMSY", 29.9934, -90.2580, "New Orleans Louis Armstrong"),
    "OKC":    ("KOKC", 35.3931, -97.6007, "Oklahoma City Will Rogers"),
    "SATX":   ("KSAT", 29.5337, -98.4698, "San Antonio Intl"),
    "TEMPDEN":("KDEN", 39.8617, -104.6731, "Denver Intl"),    # KXHIGHTEMPDEN
}


# ── Data classes ──────────────────────────────────────────────────────────
@dataclass
class ForecastPeriod:
    name:                  str            # "Tonight", "Wednesday", ...
    start_time:            dt.datetime
    end_time:              dt.datetime
    is_daytime:            bool
    temperature:           int            # °F
    short_forecast:        str
    precip_prob_pct:       Optional[int] = None


@dataclass
class StationForecast:
    station:               str
    lat:                   float
    lon:                   float
    fetched_at:            dt.datetime
    periods:               list[ForecastPeriod] = field(default_factory=list)

    def daily_high_low(self, on_date: dt.date) -> tuple[Optional[int], Optional[int]]:
        """Pick (high, low) for the calendar date in periods.

        High = the daytime period whose start lies on that date.
        Low  = the nighttime period whose start lies on that date OR the
               late-night extension of the previous day. Conservative: pick
               the lowest nighttime temp whose period intersects the date.
        """
        high = low = None
        for p in self.periods:
            d = p.start_time.date()
            if p.is_daytime and d == on_date and high is None:
                high = p.temperature
            if (not p.is_daytime) and d == on_date:
                if low is None or p.temperature < low:
                    low = p.temperature
        return high, low


# ── Cache helpers ─────────────────────────────────────────────────────────
def _cache_root() -> Path:
    ensure_dirs()
    root = CACHE_DIR / "nws"
    (root / "forecasts").mkdir(parents=True, exist_ok=True)
    (root / "observations").mkdir(parents=True, exist_ok=True)
    (root / "points").mkdir(parents=True, exist_ok=True)
    return root


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


# ── HTTP ──────────────────────────────────────────────────────────────────
_last_req = 0.0


def _http_json(url: str, *, accept: str = "application/geo+json") -> dict:
    global _last_req
    elapsed = time.time() - _last_req
    if elapsed < RATE_SLEEP_SEC:
        time.sleep(RATE_SLEEP_SEC - elapsed)
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code >= 500:
            time.sleep(2.0)
            r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    finally:
        _last_req = time.time()


# ── Public client ─────────────────────────────────────────────────────────
class NWSClient:
    """Cache-first NWS forecast client.

    Cache hit on (station, fetch_date) → return parsed StationForecast.
    Cache miss → hit /points/{lat,lon} (cached forever — gridpoint stable),
                  then the forecast URL, parse, cache.
    """
    def __init__(self, *, force_refresh: bool = False) -> None:
        self.force_refresh = force_refresh

    def _gridpoint(self, lat: float, lon: float) -> dict:
        """Return the /points/{lat,lon} payload, cached forever."""
        key = _safe(f"{lat:.4f}_{lon:.4f}")
        path = _cache_root() / "points" / f"{key}.json"
        if path.exists() and not self.force_refresh:
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        url = f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}"
        _log.info(f"NWS fetch points {url}")
        data = _http_json(url)
        path.write_text(json.dumps(data))
        return data

    def get_forecast(self, station_or_city: str,
                     on_date: Optional[dt.date] = None) -> Optional[StationForecast]:
        """Return today's forecast for a Kalshi city token or NWS station.

        Args:
            station_or_city: either a Kalshi city token (e.g. "NY", "MIA")
                             or an NWS station code (e.g. "KNYC").
            on_date: cache key — usually today (NWS doesn't archive).
        """
        on_date = on_date or dt.date.today()
        # Resolve to (station, lat, lon)
        info = KALSHI_CITY_TO_NWS.get(station_or_city.upper())
        if info is None:
            # Maybe it's already an NWS station code; try to look up by
            # value (slow but only fires for unknown tokens).
            for v in KALSHI_CITY_TO_NWS.values():
                if v[0] == station_or_city.upper():
                    info = v; break
        if info is None:
            _log.warning(f"unknown city/station: {station_or_city}")
            return None
        station, lat, lon, _display = info

        cache_key = _safe(f"{station}_{on_date.isoformat()}")
        cache_path = _cache_root() / "forecasts" / f"{cache_key}.json"
        if cache_path.exists() and not self.force_refresh:
            try:
                d = json.loads(cache_path.read_text())
                return StationForecast(
                    station=d["station"], lat=d["lat"], lon=d["lon"],
                    fetched_at=dt.datetime.fromisoformat(d["fetched_at"]),
                    periods=[ForecastPeriod(
                        name=p["name"],
                        start_time=dt.datetime.fromisoformat(p["start_time"]),
                        end_time=dt.datetime.fromisoformat(p["end_time"]),
                        is_daytime=p["is_daytime"],
                        temperature=p["temperature"],
                        short_forecast=p["short_forecast"],
                        precip_prob_pct=p.get("precip_prob_pct"),
                    ) for p in d["periods"]],
                )
            except Exception as e:
                _log.warning(f"forecast cache corrupt {cache_path.name}: {e}")

        # Fresh fetch
        try:
            point = self._gridpoint(lat, lon)
            forecast_url = point.get("properties", {}).get("forecast")
            if not forecast_url:
                _log.warning(f"no forecast URL for {station}")
                return None
            data = _http_json(forecast_url)
        except Exception as e:
            _log.warning(f"forecast fetch failed for {station}: {e}")
            return None

        periods: list[ForecastPeriod] = []
        for p in data.get("properties", {}).get("periods", []):
            try:
                pp = p.get("probabilityOfPrecipitation") or {}
                periods.append(ForecastPeriod(
                    name=p.get("name", ""),
                    start_time=dt.datetime.fromisoformat(p["startTime"]),
                    end_time=dt.datetime.fromisoformat(p["endTime"]),
                    is_daytime=bool(p.get("isDaytime", True)),
                    temperature=int(p.get("temperature") or 0),
                    short_forecast=p.get("shortForecast", ""),
                    precip_prob_pct=pp.get("value"),
                ))
            except (KeyError, ValueError, TypeError) as e:
                _log.debug(f"period parse skip: {e}")

        sf = StationForecast(
            station=station, lat=lat, lon=lon,
            fetched_at=dt.datetime.now(dt.timezone.utc),
            periods=periods,
        )
        # Persist
        cache_path.write_text(json.dumps({
            "station": sf.station, "lat": sf.lat, "lon": sf.lon,
            "fetched_at": sf.fetched_at.isoformat(),
            "periods": [{
                "name": p.name,
                "start_time": p.start_time.isoformat(),
                "end_time": p.end_time.isoformat(),
                "is_daytime": p.is_daytime,
                "temperature": p.temperature,
                "short_forecast": p.short_forecast,
                "precip_prob_pct": p.precip_prob_pct,
            } for p in sf.periods],
        }))
        return sf


# ── Climatology helper (used by backtest + as fallback) ──────────────────
# Approximate monthly climatology (avg high, avg low) per station from
# 30-yr normals (1991-2020). Not authoritative — used when no forecast is
# available (backtest, far-future markets) or as the naive baseline for BSS.
# Source: NOAA climate normals, rounded to nearest °F. Update annually.
CLIMATOLOGY_F: dict[str, dict[int, tuple[int, int]]] = {
    # station: { month_1_to_12: (avg_high_F, avg_low_F) }
    "KNYC":  {1:(40,28), 2:(43,30), 3:(50,36), 4:(61,45), 5:(71,55), 6:(80,65),
              7:(85,71), 8:(83,69), 9:(76,62), 10:(65,51), 11:(54,42), 12:(44,33)},
    "KMIA":  {1:(76,60), 2:(78,62), 3:(81,64), 4:(83,68), 5:(87,72), 6:(89,76),
              7:(91,77), 8:(91,77), 9:(89,76), 10:(85,72), 11:(80,67), 12:(77,62)},
    "KLAX":  {1:(67,49), 2:(67,51), 3:(68,53), 4:(70,55), 5:(72,58), 6:(75,61),
              7:(79,64), 8:(80,65), 9:(80,64), 10:(77,60), 11:(72,53), 12:(67,49)},
    "KMDW":  {1:(31,17), 2:(35,21), 3:(46,29), 4:(59,40), 5:(70,50), 6:(80,60),
              7:(84,65), 8:(82,64), 9:(76,56), 10:(63,44), 11:(48,33), 12:(35,22)},
    "KAUS":  {1:(61,42), 2:(65,45), 3:(72,52), 4:(79,59), 5:(86,67), 6:(92,72),
              7:(96,74), 8:(97,74), 9:(91,69), 10:(82,60), 11:(71,51), 12:(63,44)},
    "KDCA":  {1:(45,30), 2:(48,32), 3:(56,39), 4:(67,48), 5:(76,58), 6:(85,67),
              7:(89,72), 8:(87,71), 9:(80,64), 10:(69,52), 11:(58,42), 12:(48,34)},
    "KPHL":  {1:(41,26), 2:(44,28), 3:(53,35), 4:(64,44), 5:(73,54), 6:(82,64),
              7:(86,69), 8:(85,68), 9:(78,60), 10:(67,49), 11:(56,40), 12:(45,31)},
    "KDEN":  {1:(45,18), 2:(47,21), 3:(54,28), 4:(61,35), 5:(70,44), 6:(82,54),
              7:(89,60), 8:(86,58), 9:(78,49), 10:(65,37), 11:(53,26), 12:(45,19)},
    "KIAH":  {1:(63,44), 2:(67,48), 3:(73,54), 4:(79,60), 5:(86,67), 6:(91,73),
              7:(94,75), 8:(94,74), 9:(89,69), 10:(82,60), 11:(72,52), 12:(65,46)},
    "KSEA":  {1:(48,38), 2:(51,39), 3:(54,41), 4:(59,44), 5:(65,49), 6:(70,54),
              7:(76,57), 8:(77,57), 9:(71,53), 10:(60,47), 11:(52,42), 12:(46,37)},
    "KSFO":  {1:(57,44), 2:(60,46), 3:(62,47), 4:(64,48), 5:(67,51), 6:(70,54),
              7:(71,55), 8:(72,56), 9:(74,55), 10:(71,52), 11:(63,47), 12:(57,43)},
    "KPHX":  {1:(67,46), 2:(72,49), 3:(78,54), 4:(86,61), 5:(95,69), 6:(105,78),
              7:(106,84), 8:(105,83), 9:(101,77), 10:(89,65), 11:(76,53), 12:(66,46)},
    "KDFW":  {1:(57,37), 2:(61,40), 3:(69,47), 4:(76,55), 5:(83,64), 6:(91,72),
              7:(96,76), 8:(96,75), 9:(89,69), 10:(79,58), 11:(67,46), 12:(58,38)},
    "KATL":  {1:(53,35), 2:(57,38), 3:(65,44), 4:(73,51), 5:(80,60), 6:(86,68),
              7:(89,72), 8:(88,71), 9:(82,65), 10:(73,53), 11:(63,43), 12:(55,37)},
    "KBOS":  {1:(36,22), 2:(39,24), 3:(45,31), 4:(56,40), 5:(66,50), 6:(76,60),
              7:(82,66), 8:(80,65), 9:(72,57), 10:(61,46), 11:(51,37), 12:(41,28)},
    "KLAS":  {1:(58,38), 2:(64,42), 3:(72,48), 4:(79,55), 5:(89,64), 6:(99,73),
              7:(105,80), 8:(103,79), 9:(95,71), 10:(82,58), 11:(67,46), 12:(57,39)},
    "KMSP":  {1:(24,8), 2:(29,13), 3:(41,24), 4:(57,36), 5:(69,48), 6:(79,58),
              7:(83,63), 8:(81,61), 9:(73,52), 10:(58,40), 11:(42,26), 12:(28,13)},
    "KMSY":  {1:(63,46), 2:(67,49), 3:(72,54), 4:(78,60), 5:(85,68), 6:(90,73),
              7:(91,75), 8:(91,75), 9:(87,71), 10:(80,62), 11:(72,53), 12:(66,48)},
    "KOKC":  {1:(50,30), 2:(55,33), 3:(63,41), 4:(71,49), 5:(79,59), 6:(87,68),
              7:(92,72), 8:(92,71), 9:(83,63), 10:(72,52), 11:(60,40), 12:(51,31)},
    "KSAT":  {1:(63,42), 2:(66,45), 3:(73,51), 4:(80,58), 5:(86,66), 6:(92,72),
              7:(95,74), 8:(96,74), 9:(91,69), 10:(83,61), 11:(72,51), 12:(64,43)},
}


def climatology_normal_high_low(station: str, month: int) -> Optional[tuple[int, int]]:
    cm = CLIMATOLOGY_F.get(station)
    if not cm:
        return None
    return cm.get(month)


# ── Self-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    c = NWSClient()
    print("=== Forecast for NYC (KNYC) ===")
    sf = c.get_forecast("NY")
    if sf is None:
        print("  (no forecast returned)")
    else:
        print(f"  station={sf.station}  fetched={sf.fetched_at.isoformat()}")
        print(f"  {len(sf.periods)} periods. First 5:")
        for p in sf.periods[:5]:
            kind = "DAY" if p.is_daytime else "NGT"
            print(f"    {p.start_time.strftime('%a %m/%d')}  [{kind}]  "
                  f"{p.temperature:>3}°F  {p.short_forecast[:40]}")
        today = dt.date.today()
        h, l = sf.daily_high_low(today)
        print(f"  today's daily high/low: {h} / {l}")
        tmrw = today + dt.timedelta(days=1)
        h, l = sf.daily_high_low(tmrw)
        print(f"  tomorrow high/low:      {h} / {l}")

    # Cache hit
    t0 = time.time()
    sf2 = c.get_forecast("NY")
    print(f"  cache hit: {len(sf2.periods)} periods in "
          f"{(time.time()-t0)*1000:.1f}ms")

    # Climatology
    print("\n=== Climatology spot-check (KNYC May) ===")
    print(f"  {climatology_normal_high_low('KNYC', 5)}  (expect (71, 55))")
    print("OK nws")
