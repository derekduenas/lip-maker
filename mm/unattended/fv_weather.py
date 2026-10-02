"""Model fair value for Kalshi daily HIGH temperature markets (paper only).

The YES probability of one KXHIGH market (one temperature bucket or
threshold of one city-day) from an ensemble forecast plus same-day
observations. Network I/O happens only in ``WeatherHighModel`` and only on
the fair-value background thread (FairValueCache.refresh); the engine's tick
path reads the cached result. Any failure returns no value (fail closed).

Settlement facts (verified 2026-10-01; cite before changing)
------------------------------------------------------------
* Source: "the maximum temperature recorded for the specified <date>
  published in the National Weather Service's ("NWS") Daily Climate Report
  for <station>" (Kalshi contract terms, e.g.
  https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf,
  .../AUSHIGH.pdf, .../MIAHIGH.pdf; CFTC filings by KalshiEX listed per
  station in ``STATIONS``).
* Measurement day: "The NWS Climate Reports (used for daily temperature
  markets) use local standard time when reporting daily high temperatures.
  This means that during Daylight Saving Time, the high temperature will be
  recorded between 1:00 AM and 12:59 AM local time the following day."
  (https://help.kalshi.com/en/articles/13823837-weather-markets). The
  window is therefore midnight-to-midnight local STANDARD time
  (``settlement_window``).
* Units: the CLI ``MAXIMUM`` line is whole degrees Fahrenheit (e.g.
  "MAXIMUM 76 103 PM ..." in https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC).
* Payout criterion (contract terms above): "between" is inclusive of both
  degree values ("greater than or equal to the lower value ... and less than
  or equal to the greater value"); "greater than" is strictly greater;
  "less than" is strictly less. The bucket of a market is read from the
  market's own ``strike_type`` / ``floor_strike`` / ``cap_strike`` fields
  (docs.kalshi.com/api-reference/market/get-market: strike_type one of
  greater, greater_or_equal, less, less_or_equal, between, functional,
  custom, structured), never guessed from the ticker; ``strike_range``.
  Kalshi's site labels these "71° or below", "72° to 73°", "80° or above"
  (e.g. https://kalshi.com/markets/kxhighlax/highest-temperature-in-los-angeles/kxhighlax-26jun11).
* Last trading time is 11:59 PM local on <date> (NHIGH/MIAHIGH: ET; AUSHIGH:
  CT; Denver filing: MT). The engine reads close times from market metadata.

Only stations whose CLI site could be verified are in ``STATIONS``; the
others are listed in ``UNSUPPORTED`` and get no fair value.

Model
-----
1. Open-Meteo Ensemble API (free, no key; https://open-meteo.com/en/docs/ensemble-api):
   GET https://ensemble-api.open-meteo.com/v1/ensemble?latitude=40.7833&longitude=-73.9667
       &hourly=temperature_2m&models=gfs_seamless,ecmwf_ifs025
       &temperature_unit=fahrenheit&timezone=GMT&start_date=2026-10-01&end_date=2026-10-02
   Every ``temperature_2m`` / ``temperature_2m_memberNN`` series (suffixed
   with the model name when several models are asked for) is one member.
   Each member's value is its max over the hourly values inside the
   settlement window (or, once the day has started, over the remaining
   hours; see 2). Hourly instants under-read the true max between hours:
   that is what the per-station ``bias_f`` is for (default 0, to be fitted).
2. Same-day observations, once the window has started:
   GET https://api.weather.gov/stations/KNYC/observations?start=2026-10-01T05:00:00Z
   (User-Agent required: https://weather.gov/documentation/services-web-api).
   The max observed so far minus ``LIP_FV_WX_OBS_MARGIN_F`` (0.5 F: hourly
   METAR values converted from Celsius can round above the CLI value) floors
   the distribution: probability mass below it is moved onto it.
3. Members -> probability: mean-preserving spread inflation, bias shift,
   Gaussian kernel (sd ``kernel_sd_f``, default 1.0 F) around each member,
   integer rounding exactly as the CLI reports (whole F: the market's integer
   range [lo, hi] is the continuous interval [lo - 0.5, hi + 0.5)), averaged
   over members and clipped to [0.01, 0.99].
4. Confidence in [0, 1]: lower for long lead times, few members, and a
   failed observation fetch on a started day (``confidence``).

Per-station parameters come from the JSON file ``LIP_FV_WX_PARAMS_FILE``
({"KXHIGHNY": {"bias_f": 0.0, "inflation": 1.0, "kernel_sd_f": 1.0}}), so
they can be fitted later; an unreadable file means no weather fair value.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("lip.fairvalue")

SOURCE = "open_meteo_ensemble_high"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
NWS_OBS_URL = "https://api.weather.gov/stations/{station}/observations"
DEFAULT_MODELS = "gfs_seamless,ecmwf_ifs025"
DEFAULT_USER_AGENT = "(lip-maker paper fair value, github.com/derekduenas/lip-maker)"
P_MIN, P_MAX = 0.01, 0.99


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass(frozen=True)
class Station:
    series: str
    name: str      # NWS Daily Climate Report (CLI) site
    cli: str       # CLI product "issuedby" id
    wfo: str       # issuing NWS office
    icao: str      # ASOS station for api.weather.gov observations
    lat: float
    lon: float
    tz: str        # IANA zone; the standard-time offset defines the window
    terms: str     # Kalshi contract terms naming the CLI site
    site_src: str  # NWS page tying the CLI site to the ASOS station


# Coordinates: NWS station headers at https://tgftp.nws.noaa.gov/weather/current/<ICAO>.html
# (degrees-minutes, converted: 40-47N 073-58W -> 40.7833, -73.9667).
STATIONS: dict[str, Station] = {
    "KXHIGHNY": Station(
        "KXHIGHNY", "Central Park, New York", "NYC", "OKX", "KNYC", 40.7833, -73.9667, "America/New_York",
        "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf",
        "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC "
        "('THE CENTRAL PARK NY CLIMATE SUMMARY'); https://tgftp.nws.noaa.gov/weather/current/KNYC.html "
        "'(KNYC) 40-47N 073-58W 48M'"),
    "KXHIGHCHI": Station(
        "KXHIGHCHI", "Chicago Midway, Illinois", "MDW", "LOT", "KMDW", 41.7833, -87.75, "America/Chicago",
        "https://www.cftc.gov/sites/default/files/filings/orgrules/23/04/rules042623988.pdf "
        "('Daily Climate Report for Chicago Midway, Illinois')",
        "https://forecast.weather.gov/product.php?site=LOT&product=CLI&issuedby=MDW "
        "('THE CHICAGO-MIDWAY CLIMATE SUMMARY'); https://tgftp.nws.noaa.gov/weather/current/KMDW.html "
        "'(KMDW) 41-47N 087-45W 188M'"),
    "KXHIGHAUS": Station(
        "KXHIGHAUS", "Austin Bergstrom", "AUS", "EWX", "KAUS", 30.1833, -97.6833, "America/Chicago",
        "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/AUSHIGH.pdf",
        "https://forecast.weather.gov/product.php?site=EWX&issuedby=AUS&product=CLI "
        "('THE AUSTIN BERGSTROM CLIMATE SUMMARY'); https://tgftp.nws.noaa.gov/weather/current/KAUS.html "
        "'(KAUS) 30-11N 097-41W 172M'"),
    "KXHIGHDEN": Station(
        "KXHIGHDEN", "Denver, CO (Denver International Airport)", "DEN", "BOU", "KDEN", 39.8667, -104.6667,
        "America/Denver",
        "https://www.cftc.gov/sites/default/files/filings/ptc/24/10/ptc1018247238.pdf "
        "('Daily Climate Report for Denver, CO')",
        "https://forecast.weather.gov/product.php?site=NWS&product=CLI&issuedby=DEN ('THE DENVER CO "
        "CLIMATE SUMMARY'); https://www.weather.gov/bou/Climate_Record_October ('at Denver International "
        "Airport from March 1995 to Present'); "
        "https://tgftp.nws.noaa.gov/data/observations/metar/decoded/KDEN.TXT '(KDEN) 39-52N 104-40W 1640M'"),
    "KXHIGHPHIL": Station(
        "KXHIGHPHIL", "Philadelphia, PA", "PHL", "PHI", "KPHL", 39.8667, -75.2333, "America/New_York",
        "https://www.cftc.gov/filings/ptc/ptc1018247256.pdf ('Daily Climate Report for Philadelphia, PA')",
        "https://forecast.weather.gov/product.php?site=PHI&product=CLI&issuedby=PHL ('THE PHILADELPHIA PA "
        "CLIMATE SUMMARY'); https://tgftp.nws.noaa.gov/weather/current/KPHL.html '(KPHL) 39-52N 075-14W 18M'"),
    "KXHIGHMIA": Station(
        "KXHIGHMIA", "Miami, FL", "MIA", "MFL", "KMIA", 25.7833, -80.3167, "America/New_York",
        "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/MIAHIGH.pdf",
        "https://forecast.weather.gov/product.php?site=MFL&issuedby=MIA&product=CLI ('THE MIAMI CLIMATE "
        "SUMMARY'); https://www.weather.gov/mfl/rodney1 ('based on temperature readings at Miami "
        "International Airport'); https://tgftp.nws.noaa.gov/weather/current/KMIA.html '(KMIA) 25-47N 080-19W 8M'"),
}

# Series seen on https://kalshi.com/category/climate/daily-temperature whose CLI
# site could not be verified from Kalshi's own terms: no fair value.
UNSUPPORTED: dict[str, str] = {
    "KXHIGHLAX": "conflicting sources: KalshiEX filing ptc1018247244 names 'Downtown Los Angeles, CA'; "
                 "third-party pages say LAX; Kalshi series settlement_sources not reachable from here",
    **{s: "settlement station not verified from Kalshi terms" for s in (
        "KXHIGHTSATX", "KXHIGHTOKC", "KXHIGHTATL", "KXHIGHTSFO", "KXHIGHTMIN", "KXHIGHTDAL",
        "KXHIGHTPHX", "KXHIGHTLV", "KXHIGHTSEA", "KXHIGHTHOU", "KXHIGHTBOS", "KXHIGHTDC")},
}


def station_for(series: str) -> Station | None:
    return STATIONS.get(str(series or "").upper())


_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_DAY = re.compile(r"^(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})$")


def measurement_date(ticker: str) -> date | None:
    """<date> of a KXHIGH market from its event segment (KXHIGHNY-26OCT01-B72.5
    -> 2026-10-01). None when the second segment is not YYMONDD."""
    parts = str(ticker or "").upper().split("-")
    if len(parts) < 2:
        return None
    m = _DAY.match(parts[1])
    if not m:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def title_date_ok(title: str | None, d: date) -> bool:
    """False only when a market title names a different month/day than ``d``
    (cross-check of the ticker date; a title without a date passes)."""
    if not title:
        return True
    m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2})\b", title)
    if not m:
        return True
    return (_MONTHS[m.group(1).upper()], int(m.group(2))) == (d.month, d.day)


def standard_offset(tz: str, d: date) -> timedelta:
    """UTC offset of the zone's STANDARD time on ``d`` (DST removed)."""
    from zoneinfo import ZoneInfo
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(tz))
    return noon.utcoffset() - (noon.dst() or timedelta(0))


def settlement_window(station: Station, d: date) -> tuple[float, float]:
    """(start, end) epoch seconds: ``d`` 00:00 to ``d``+1 00:00 local standard
    time (NYC in October: 05:00Z to 05:00Z = 1:00 AM to 1:00 AM EDT)."""
    off = standard_offset(station.tz, d)
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - off
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def strike_range(strike_type, floor_strike, cap_strike) -> tuple[int | None, int | None] | None:
    """Integer CLI values [lo, hi] (None = unbounded) that settle YES, from the
    market's strike fields under the contract's payout criterion. None when
    the fields are missing, unknown or empty."""
    def num(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None
    st = str(strike_type or "").lower()
    f, c = num(floor_strike), num(cap_strike)
    if st == "greater" and f is not None:
        rng = (math.floor(f) + 1, None)
    elif st == "greater_or_equal" and f is not None:
        rng = (math.ceil(f), None)
    elif st == "less" and c is not None:
        rng = (None, math.ceil(c) - 1)
    elif st == "less_or_equal" and c is not None:
        rng = (None, math.floor(c))
    elif st == "between" and f is not None and c is not None:
        rng = (math.ceil(f), math.floor(c))
    else:
        return None
    lo, hi = rng
    if lo is not None and hi is not None and lo > hi:
        return None
    return rng


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob(values: list[float], lo: int | None, hi: int | None, *, sd: float = 1.0,
                bias: float = 0.0, inflation: float = 1.0, floor_f: float | None = None) -> float:
    """P(lo <= CLI max <= hi), unclipped. Each member v is shifted to
    mean + inflation x (v - mean) + bias and smoothed with N(., sd); the CLI
    integer k collects [k - 0.5, k + 0.5). Mass below ``floor_f`` is moved
    onto ``floor_f`` (the observed max so far)."""
    if not values:
        raise ValueError("no members")
    if not sd > 0:
        raise ValueError("kernel sd must be > 0")
    mean = sum(values) / len(values)

    def cdf(x: float, mu: float) -> float:
        if floor_f is not None and x <= floor_f:
            return 0.0
        return _phi((x - mu) / sd)

    total = 0.0
    for v in values:
        mu = mean + inflation * (v - mean) + bias
        upper = 1.0 if hi is None else cdf(hi + 0.5, mu)
        lower = 0.0 if lo is None else cdf(lo - 0.5, mu)
        total += max(0.0, upper - lower)
    return total / len(values)


def clip_prob(p: float) -> float:
    return min(P_MAX, max(P_MIN, float(p)))


def confidence(lead_h: float, n_members: int, obs_status: str) -> float:
    """Heuristic score in [0, 1] (not a calibrated probability):
    lead factor 1 - lead/LIP_FV_WX_CONF_HORIZON_H (240 h), floored at 0.1;
    member factor n/LIP_FV_WX_FULL_MEMBERS (40), capped at 1; x0.5 when the
    day has started and the observation fetch failed."""
    horizon = max(1.0, _env("LIP_FV_WX_CONF_HORIZON_H", 240.0))
    full = max(1.0, _env("LIP_FV_WX_FULL_MEMBERS", 40.0))
    lead_f = min(1.0, max(0.1, 1.0 - max(0.0, lead_h) / horizon))
    mem_f = min(1.0, max(0.0, n_members / full))
    obs_f = 0.5 if obs_status == "failed" else 1.0
    return round(lead_f * mem_f * obs_f, 4)


_MEMBER_KEY = re.compile(r"^temperature_2m(?:_member\d+)?(?:_[a-z0-9_]+)?$")


def _utc_ts(raw: str) -> float:
    s = str(raw).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_ensemble(payload: dict) -> tuple[list[float], list[list]]:
    """(hourly UTC epoch times, member series) from an Open-Meteo ensemble
    response requested with timezone=GMT and temperature_unit=fahrenheit.
    Raises ValueError on a response in another zone or unit."""
    if int(payload.get("utc_offset_seconds") or 0) != 0:
        raise ValueError("ensemble response not in GMT")
    hourly = payload.get("hourly") or {}
    units = payload.get("hourly_units") or {}
    times = [_utc_ts(t) for t in hourly.get("time") or []]
    members = []
    for key in sorted(hourly):
        if key == "time" or not _MEMBER_KEY.match(key):
            continue
        unit = units.get(key)
        if unit is not None and "F" not in str(unit):
            raise ValueError(f"ensemble unit {unit!r} is not Fahrenheit")
        series = hourly[key]
        if isinstance(series, list) and len(series) == len(times):
            members.append(series)
    return times, members


def member_maxes(times: list[float], members: list[list], start: float, end: float) -> list[float]:
    """Each member's max over the hourly instants in [start, end). A member
    missing any hour of that span is dropped; an empty span gives []."""
    need = int(round((end - start) / 3600.0))
    idx = [i for i, t in enumerate(times) if start <= t < end]
    if need <= 0 or len(idx) < need:
        return []
    out = []
    for series in members:
        vals = [series[i] for i in idx]
        if any(v is None for v in vals):
            continue
        out.append(max(float(v) for v in vals))
    return out


def obs_max_f(payload: dict, start: float, now: float) -> float | None:
    """Max temperature (F) among api.weather.gov observations timestamped in
    [start, now]. Values in degC are converted; other units are skipped."""
    best = None
    for feat in payload.get("features") or []:
        props = (feat or {}).get("properties") or {}
        temp = props.get("temperature") or {}
        val = temp.get("value")
        if val is None or not props.get("timestamp"):
            continue
        try:
            ts = _utc_ts(props["timestamp"])
            v = float(val)
        except (TypeError, ValueError):
            continue
        if not (start <= ts <= now):
            continue
        unit = str(temp.get("unitCode") or "")
        if unit.endswith("degC"):
            v = v * 9.0 / 5.0 + 32.0
        elif not unit.endswith("degF"):
            continue
        best = v if best is None else max(best, v)
    return best


DEFAULT_PARAMS = {"bias_f": 0.0, "inflation": 1.0}


def load_params(path: str | None) -> dict:
    """Per-series model parameters from JSON. Missing path -> {}. A file that
    cannot be read or has non-numeric values raises (caller fails closed)."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("params file must be an object")
    out = {}
    for series, row in raw.items():
        if not isinstance(row, dict):
            raise ValueError(f"params for {series} must be an object")
        clean = {}
        for k in ("bias_f", "inflation", "kernel_sd_f"):
            if k in row:
                v = float(row[k])
                if not math.isfinite(v):
                    raise ValueError(f"{series}.{k} not finite")
                clean[k] = v
        if clean.get("kernel_sd_f", 1.0) <= 0 or clean.get("inflation", 1.0) <= 0:
            raise ValueError(f"{series}: kernel_sd_f and inflation must be > 0")
        out[str(series).upper()] = clean
    return out


def lead_bucket(lead_h: float) -> str:
    """Lead-time bucket (hours from the FV to the end of the settlement window)."""
    if lead_h < 12:
        return "0-12h"
    if lead_h < 24:
        return "12-24h"
    if lead_h < 48:
        return "24-48h"
    return "48h+"


class WeatherHighModel:
    """Fetch + cache + price. Call only from the fair-value background thread."""

    def __init__(self, http, *, clock=time.time) -> None:
        self.http = http
        self.clock = clock
        self.ens_cache: dict = {}
        self.obs_cache: dict = {}
        self.stats = {"priced": 0, "unsupported": 0, "no_strike": 0, "errors": 0,
                      "ens_fetches": 0, "obs_fetches": 0, "last_error": None}

    # -- fetches (background thread only)
    def ensemble(self, st: Station, d: date, start: float, end: float, now: float):
        key = (st.series, d.isoformat())
        hit = self.ens_cache.get(key)
        if hit is not None and now - hit[0] < _env("LIP_FV_WX_ENS_TTL_S", 1800.0):
            return hit[1], hit[2]
        params = {"latitude": f"{st.lat:.4f}", "longitude": f"{st.lon:.4f}", "hourly": "temperature_2m",
                  "models": os.environ.get("LIP_FV_WX_MODELS") or DEFAULT_MODELS,
                  "temperature_unit": "fahrenheit", "timezone": "GMT",
                  "start_date": datetime.fromtimestamp(start, timezone.utc).date().isoformat(),
                  "end_date": datetime.fromtimestamp(end, timezone.utc).date().isoformat()}
        r = self.http.get(ENSEMBLE_URL, params=params, timeout=_env("LIP_FV_WX_TIMEOUT_S", 15.0))
        self.stats["ens_fetches"] += 1
        if r.status_code != 200:
            raise ValueError(f"ensemble HTTP {r.status_code}")
        times, members = parse_ensemble(r.json())
        self.ens_cache[key] = (now, times, members)
        for k in [k for k, v in self.ens_cache.items() if now - v[0] > 86400.0]:
            del self.ens_cache[k]
        return times, members

    def observed_max(self, st: Station, start: float, now: float):
        """Max observed F so far in the window, or raises."""
        key = (st.icao, int(start))
        hit = self.obs_cache.get(key)
        if hit is not None and now - hit[0] < _env("LIP_FV_WX_OBS_TTL_S", 300.0):
            return hit[1]
        url = NWS_OBS_URL.format(station=st.icao)
        r = self.http.get(url, params={"start": datetime.fromtimestamp(start, timezone.utc)
                                       .strftime("%Y-%m-%dT%H:%M:%SZ")},
                          headers={"User-Agent": os.environ.get("LIP_FV_NWS_USER_AGENT") or DEFAULT_USER_AGENT,
                                   "Accept": "application/geo+json"},
                          timeout=_env("LIP_FV_WX_TIMEOUT_S", 15.0))
        self.stats["obs_fetches"] += 1
        if r.status_code != 200:
            raise ValueError(f"observations HTTP {r.status_code}")
        mx = obs_max_f(r.json(), start, now)
        self.obs_cache[key] = (now, mx)
        for k in [k for k, v in self.obs_cache.items() if now - v[0] > 86400.0]:
            del self.obs_cache[k]
        return mx

    # -- pricing
    def fv_for(self, market: str, meta: dict, now: float | None = None, params: dict | None = None):
        """Fair-value row for ``market`` or None (unsupported, bad metadata,
        out of range, fetch failure: fail closed)."""
        now = self.clock() if now is None else float(now)
        series = str(market).split("-", 1)[0].upper()
        st = station_for(series)
        if st is None:
            self.stats["unsupported"] += 1
            return None
        d = measurement_date(market)
        rng = strike_range(meta.get("strike_type"), meta.get("floor_strike"), meta.get("cap_strike"))
        if d is None or rng is None or not title_date_ok(meta.get("title"), d):
            self.stats["no_strike"] += 1
            return None
        start, end = settlement_window(st, d)
        lead_h = max(0.0, (end - now) / 3600.0)
        if (start - now) / 3600.0 > _env("LIP_FV_WX_MAX_LEAD_H", 168.0):
            return None
        if now > end + 36 * 3600.0:
            return None  # long past the window: nothing left to price
        p = dict(DEFAULT_PARAMS, kernel_sd_f=_env("LIP_FV_WX_KERNEL_SD", 1.0))
        p.update((params or {}).get(series) or {})
        try:
            obs_status, floor_f, obs = "not_started", None, None
            if now >= start:
                try:
                    obs = self.observed_max(st, start, now)
                    obs_status = "ok" if obs is not None else "failed"
                except Exception as exc:
                    obs_status = "failed"
                    self.stats["last_error"] = f"obs {type(exc).__name__}"
                if obs is not None:
                    floor_f = obs - _env("LIP_FV_WX_OBS_MARGIN_F", 0.5)
            if now >= end:
                if obs is None:
                    return None
                values, n = [obs], int(_env("LIP_FV_WX_FULL_MEMBERS", 40.0))
            else:
                times, members = self.ensemble(st, d, start, end, now)
                from_ts = max(start, math.floor(now / 3600.0) * 3600.0)
                values = member_maxes(times, members, from_ts, end)
                n = len(values)
                if n < int(_env("LIP_FV_WX_MIN_MEMBERS", 10.0)):
                    return None
            prob = bucket_prob(values, rng[0], rng[1], sd=float(p["kernel_sd_f"]), bias=float(p["bias_f"]),
                               inflation=float(p["inflation"]), floor_f=floor_f)
        except Exception as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            return None
        self.stats["priced"] += 1
        fv = round(clip_prob(prob) * 100.0, 2)
        lo, hi = rng
        label = (f"{lo}F or above" if hi is None else f"{hi}F or below" if lo is None else f"{lo}F to {hi}F")
        return {"fv_cents": fv, "conf": confidence(lead_h, n, obs_status), "source": SOURCE,
                "pm_question": f"{st.icao} CLI max {d.isoformat()} {label} ({n} members)",
                "station": series, "lead_h": round(lead_h, 2), "members": n, "obs_status": obs_status,
                "obs_max_f": None if obs is None else round(obs, 1), "range": [lo, hi],
                "window": [start, end], "ts": now}
