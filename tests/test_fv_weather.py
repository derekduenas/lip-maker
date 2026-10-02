"""Weather (daily HIGH) fair-value model: facts, probability math, fetch
paths. Network is never touched: responses are synthetic fixtures shaped like
the documented Open-Meteo ensemble and api.weather.gov observation payloads."""
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from mm.unattended import fv_weather as W

FIX = Path(__file__).parent / "fixtures" / "fv_weather"


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------- facts
def test_supported_stations_and_unsupported_list():
    assert set(W.STATIONS) == {"KXHIGHNY", "KXHIGHCHI", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHPHIL", "KXHIGHMIA"}
    assert W.STATIONS["KXHIGHNY"].icao == "KNYC" and W.STATIONS["KXHIGHCHI"].icao == "KMDW"
    assert W.station_for("kxhighny").name.startswith("Central Park")
    assert W.station_for("KXHIGHLAX") is None and "KXHIGHLAX" in W.UNSUPPORTED
    for st in W.STATIONS.values():
        assert st.terms.startswith("https://") and "https://" in st.site_src


def test_measurement_date_from_event_segment():
    assert W.measurement_date("KXHIGHNY-26OCT01-B72.5") == date(2026, 10, 1)
    assert W.measurement_date("KXHIGHCHI-26FEB29-T40") is None  # not a date
    assert W.measurement_date("KXHIGHNY") is None
    assert W.title_date_ok("Highest temperature in NYC on Oct 1, 2026?", date(2026, 10, 1))
    assert not W.title_date_ok("Highest temperature in NYC on Oct 2, 2026?", date(2026, 10, 1))
    assert W.title_date_ok(None, date(2026, 10, 1))


# ---------------------------------------------------------------- window / DST
def test_window_is_local_standard_time_during_dst():
    # NYC, 1 Oct 2026 (EDT): LST midnight = 05:00Z -> 1:00 AM to 1:00 AM EDT.
    s, e = W.settlement_window(W.STATIONS["KXHIGHNY"], date(2026, 10, 1))
    assert s == _utc(2026, 10, 1, 5) and e == _utc(2026, 10, 2, 5)
    # Denver MST = UTC-7 all year.
    s, e = W.settlement_window(W.STATIONS["KXHIGHDEN"], date(2026, 7, 4))
    assert s == _utc(2026, 7, 4, 7)


def test_window_same_utc_offset_in_winter_and_on_transition_days():
    st = W.STATIONS["KXHIGHCHI"]
    assert W.settlement_window(st, date(2026, 1, 15))[0] == _utc(2026, 1, 15, 6)
    # 2026-03-08 (DST starts) and 2026-11-01 (DST ends): still 24 h of CST.
    for d in (date(2026, 3, 8), date(2026, 11, 1)):
        s, e = W.settlement_window(st, d)
        assert s == _utc(d.year, d.month, d.day, 6) and e - s == 86400


# ---------------------------------------------------------------- strikes
@pytest.mark.parametrize("st,f,c,want", [
    ("greater", 85, None, (86, None)),        # "86 or above" (strictly > 85)
    ("less", None, 84, (None, 83)),           # "83 or below" (strictly < 84)
    ("between", 84, 85, (84, 85)),            # "84 to 85" inclusive
    ("greater_or_equal", 85, None, (85, None)),
    ("less_or_equal", None, 84, (None, 84)),
    ("greater", 85.5, None, (86, None)),
    ("between", 85, 84, None),
    ("functional", 1, 2, None),
    ("between", None, 85, None),
    (None, 1, 2, None),
])
def test_strike_range(st, f, c, want):
    assert W.strike_range(st, f, c) == want


# ---------------------------------------------------------------- probability
def test_rounding_bucket_edges_with_tiny_kernel():
    # A member at exactly 84.4 rounds to 84; 84.6 to 85 (CLI whole degrees).
    p = W.bucket_prob([84.4], None, 84, sd=1e-6)
    assert p == pytest.approx(1.0)
    assert W.bucket_prob([84.6], None, 84, sd=1e-6) == pytest.approx(0.0)
    assert W.bucket_prob([84.6], 85, 86, sd=1e-6) == pytest.approx(1.0)


def test_kernel_and_buckets_partition_to_one():
    vals = [70.0, 72.5, 74.0, 75.2, 79.0]
    edges = [(None, 71), (72, 73), (74, 75), (76, 77), (78, None)]
    ps = [W.bucket_prob(vals, lo, hi, sd=1.0) for lo, hi in edges]
    assert sum(ps) == pytest.approx(1.0, abs=1e-12)
    # one member at 72.5 with sd 1: P(71.5 <= T < 73.5) = Phi(1) - Phi(-1)
    assert W.bucket_prob([72.5], 72, 73, sd=1.0) == pytest.approx(math.erf(1 / math.sqrt(2)), abs=1e-12)


def test_bias_inflation_and_threshold_complement():
    vals = [80.0, 82.0, 84.0]
    above = W.bucket_prob(vals, 83, None, sd=1.0)
    below = W.bucket_prob(vals, None, 82, sd=1.0)
    assert above + below == pytest.approx(1.0)
    assert W.bucket_prob(vals, 83, None, sd=1.0, bias=2.0) > above
    # inflation spreads mass into the tails, keeping the mean
    assert W.bucket_prob(vals, 86, None, sd=1.0, inflation=2.0) > W.bucket_prob(vals, 86, None, sd=1.0)


def test_observation_floor_moves_mass_up():
    vals = [70.0] * 10
    assert W.bucket_prob(vals, None, 71, sd=1.0) > 0.8
    # observed 74.6 F prints as 75 in the CLI: the floor is that whole degree
    floored = W.bucket_prob(vals, None, 71, sd=1.0, floor_f=W.cli_round_f(74.6))
    assert floored == 0.0
    assert W.bucket_prob(vals, 74, 75, sd=1.0, floor_f=75) == pytest.approx(1.0)


def test_clip_and_bad_inputs():
    assert W.clip_prob(0.0) == 0.01 and W.clip_prob(1.0) == 0.99 and W.clip_prob(0.5) == 0.5
    with pytest.raises(ValueError):
        W.bucket_prob([], 1, 2)
    with pytest.raises(ValueError):
        W.bucket_prob([70.0], 1, 2, sd=0)


def test_confidence_lower_for_lead_members_and_obs_failure():
    full = W.confidence(6, 82, "ok")
    assert full == 1.0 or full > 0.95
    assert W.confidence(120, 82, "ok") < W.confidence(24, 82, "ok")
    assert W.confidence(24, 20, "ok") < W.confidence(24, 82, "ok")
    assert W.confidence(24, 82, "failed") == pytest.approx(W.confidence(24, 82, "ok") * 0.5, abs=1e-3)
    assert W.confidence(10_000, 82, "ok") == pytest.approx(0.1)


# ---------------------------------------------------------------- parsing
def test_parse_fixture_and_member_maxes():
    payload = json.loads((FIX / "ensemble_knyc_2026-10-01.json").read_text())
    times, members = W.parse_ensemble(payload)
    assert len(members) == 4  # 2 models x (control + 1 member) in the fixture
    s, e = _utc(2026, 10, 1, 5), _utc(2026, 10, 2, 5)
    maxes = W.member_maxes(times, members, s, e)
    assert len(maxes) == 3  # the member with a missing hour inside the window is dropped
    assert max(maxes) == 76.0


def test_parse_rejects_wrong_zone_or_unit():
    with pytest.raises(ValueError):
        W.parse_ensemble({"utc_offset_seconds": -14400, "hourly": {}})
    with pytest.raises(ValueError):
        W.parse_ensemble({"hourly": {"time": ["2026-10-01T00:00"], "temperature_2m": [20.0]},
                          "hourly_units": {"temperature_2m": "°C"}})


def test_obs_max_converts_celsius_and_respects_window():
    payload = json.loads((FIX / "obs_knyc_2026-10-01.json").read_text())
    s = _utc(2026, 10, 1, 5)
    # 24.4 C = 75.92 F at 18:51Z; the 25.0 C row is before the window; null rows skipped.
    assert W.obs_max_f(payload, s, _utc(2026, 10, 1, 20)) == pytest.approx(75.92)
    assert W.obs_max_f(payload, s, _utc(2026, 10, 1, 12)) == pytest.approx(64.04)
    assert W.obs_max_f({"features": []}, s, _utc(2026, 10, 1, 12)) is None


def test_load_params(tmp_path):
    assert W.load_params(None) == {}
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"kxhighny": {"bias_f": 0.7, "inflation": 1.2, "kernel_sd_f": 1.5}}))
    assert W.load_params(str(p)) == {"KXHIGHNY": {"bias_f": 0.7, "inflation": 1.2, "kernel_sd_f": 1.5}}
    p.write_text(json.dumps({"KXHIGHNY": {"kernel_sd_f": 0}}))
    with pytest.raises(ValueError):
        W.load_params(str(p))
    p.write_text("{not json")
    with pytest.raises(ValueError):
        W.load_params(str(p))


# ---------------------------------------------------------------- model with fake http
class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


class _Http:
    def __init__(self, ens=None, obs=None, ens_code=200, obs_code=200, raise_obs=False):
        self.ens, self.obs, self.ens_code, self.obs_code, self.raise_obs = ens, obs, ens_code, obs_code, raise_obs
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {}), timeout))
        if url == W.ENSEMBLE_URL:
            return _Resp(self.ens_code, self.ens)
        if self.raise_obs:
            raise TimeoutError("obs timeout")
        return _Resp(self.obs_code, self.obs)


def _ens(values_by_member, start=_utc(2026, 10, 1, 0), hours=48):
    times = [datetime.fromtimestamp(start + 3600 * i, timezone.utc).strftime("%Y-%m-%dT%H:%M") for i in range(hours)]
    hourly = {"time": times}
    units = {"time": "iso8601"}
    for i, v in enumerate(values_by_member):
        key = "temperature_2m" if i == 0 else f"temperature_2m_member{i:02d}"
        hourly[key] = [v] * hours if not callable(v) else [v(t) for t in range(hours)]
        units[key] = "°F"
    return {"utc_offset_seconds": 0, "hourly": hourly, "hourly_units": units}


META_B = {"strike_type": "between", "floor_strike": 72, "cap_strike": 73}


def test_model_prices_bucket_and_documents_request():
    http = _Http(ens=_ens([72.5] * 30))
    m = W.WeatherHighModel(http)
    now = _utc(2026, 9, 30, 15)
    row = m.fv_for("KXHIGHNY-26OCT01-B72.5", META_B, now=now)
    assert row["source"] == W.SOURCE and row["members"] == 30
    # unfitted default kernel sd 1.75 F (identical members: inflation has no effect)
    assert row["fv_cents"] == pytest.approx(round(math.erf(1 / 1.75 / math.sqrt(2)) * 100, 2))
    assert row["lead_h"] == pytest.approx(38.0) and 0 < row["conf"] < 1
    url, params, _h, timeout = http.calls[0]
    assert url == "https://ensemble-api.open-meteo.com/v1/ensemble"
    assert params == {"latitude": "40.7833", "longitude": "-73.9667", "hourly": "temperature_2m",
                      "models": "gfs025,ecmwf_ifs025", "temperature_unit": "fahrenheit",
                      "timezone": "GMT", "start_date": "2026-10-01", "end_date": "2026-10-02"}
    assert timeout == 15.0
    # cached: a second call in the TTL does not refetch
    m.fv_for("KXHIGHNY-26OCT01-T75", {"strike_type": "greater", "floor_strike": 75}, now=now + 60)
    assert len(http.calls) == 1


def test_model_clips_probabilities():
    m = W.WeatherHighModel(_Http(ens=_ens([60.0] * 30)))
    row = m.fv_for("KXHIGHNY-26OCT01-T85", {"strike_type": "greater", "floor_strike": 85},
                   now=_utc(2026, 9, 30, 15))
    assert row["fv_cents"] == 1.0
    row = m.fv_for("KXHIGHNY-26OCT01-T85", {"strike_type": "less", "cap_strike": 85},
                   now=_utc(2026, 9, 30, 15))
    assert row["fv_cents"] == 99.0


def test_model_uses_observation_floor_and_user_agent():
    # Members say 70F for the rest of the day, but 75.9F was already observed.
    obs = json.loads((FIX / "obs_knyc_2026-10-01.json").read_text())
    http = _Http(ens=_ens([70.0] * 30), obs=obs)
    m = W.WeatherHighModel(http)
    now = _utc(2026, 10, 1, 20)
    row = m.fv_for("KXHIGHNY-26OCT01-T71", {"strike_type": "less", "cap_strike": 71}, now=now)
    assert row["fv_cents"] == 1.0 and row["obs_status"] == "ok" and row["obs_max_f"] == 75.9
    obs_call = [c for c in http.calls if c[0] != W.ENSEMBLE_URL][0]
    assert obs_call[0] == "https://api.weather.gov/stations/KNYC/observations"
    assert obs_call[1] == {"start": "2026-10-01T05:00:00Z"}
    assert obs_call[2]["User-Agent"]


def test_model_obs_failure_on_a_started_day_prices_nothing():
    # The remaining-hours member max alone would ignore the hours already past.
    http = _Http(ens=_ens([70.0] * 30), raise_obs=True)
    m = W.WeatherHighModel(http)
    now = _utc(2026, 10, 1, 20)
    assert m.fv_for("KXHIGHNY-26OCT01-T71", {"strike_type": "less", "cap_strike": 71}, now=now) is None
    assert m.stats["no_obs"] == 1


@pytest.mark.parametrize("market,meta", [
    ("KXHIGHLAX-26OCT01-B72.5", META_B),                     # unsupported station
    ("KXHIGHNY-26OCT01-B72.5", {}),                          # no strike metadata
    ("KXHIGHNY-26OCT01-B72.5", {"strike_type": "custom"}),   # unknown strike type
    ("KXHIGHNY-26OCT01-B72.5", dict(META_B, title="Highest temperature in NYC on Oct 2, 2026?")),
])
def test_model_fail_closed_on_metadata(market, meta):
    m = W.WeatherHighModel(_Http(ens=_ens([72.5] * 30)))
    assert m.fv_for(market, meta, now=_utc(2026, 9, 30, 15)) is None


def test_model_fail_closed_on_api_failures():
    now = _utc(2026, 9, 30, 15)
    assert W.WeatherHighModel(_Http(ens={}, ens_code=503)).fv_for("KXHIGHNY-26OCT01-B72.5", META_B, now=now) is None
    few = W.WeatherHighModel(_Http(ens=_ens([72.5] * 5)))
    assert few.fv_for("KXHIGHNY-26OCT01-B72.5", META_B, now=now) is None
    short = W.WeatherHighModel(_Http(ens=_ens([72.5] * 30, hours=20)))  # window not covered
    assert short.fv_for("KXHIGHNY-26OCT01-B72.5", META_B, now=now) is None
    bad = W.WeatherHighModel(_Http(ens={"utc_offset_seconds": 3600, "hourly": {}}))
    assert bad.fv_for("KXHIGHNY-26OCT01-B72.5", META_B, now=now) is None
    assert bad.stats["errors"] == 1
    far = W.WeatherHighModel(_Http(ens=_ens([72.5] * 30)))
    assert far.fv_for("KXHIGHNY-26OCT20-B72.5", META_B, now=now) is None  # beyond max lead


def test_model_after_window_uses_observed_max_only():
    obs = json.loads((FIX / "obs_knyc_2026-10-01.json").read_text())
    http = _Http(ens=None, obs=obs)
    m = W.WeatherHighModel(http)
    row = m.fv_for("KXHIGHNY-26OCT01-B75.5", {"strike_type": "between", "floor_strike": 75, "cap_strike": 76},
                   now=_utc(2026, 10, 2, 6))
    # observed 75.92F prints as 76: mass below 75.5 goes to 76 (0.9) and 75
    # (0.1), both in 75-76; kernel sd 1.75 above: P = Phi((76.5 - 75.92) / 1.75)
    assert row["fv_cents"] == pytest.approx(100 * 0.5 * (1 + math.erf(0.58 / 1.75 / math.sqrt(2))), abs=0.01)
    assert row["lead_h"] == 0
    low = m.fv_for("KXHIGHNY-26OCT01-T75", {"strike_type": "less", "cap_strike": 75}, now=_utc(2026, 10, 2, 6))
    assert low["fv_cents"] == 1.0
    assert all(c[0] != W.ENSEMBLE_URL for c in http.calls)


def test_params_per_station_change_price():
    m = W.WeatherHighModel(_Http(ens=_ens([72.5] * 30)))
    now = _utc(2026, 9, 30, 15)
    base = m.fv_for("KXHIGHNY-26OCT01-T73", {"strike_type": "greater", "floor_strike": 73}, now=now)
    shifted = m.fv_for("KXHIGHNY-26OCT01-T73", {"strike_type": "greater", "floor_strike": 73}, now=now,
                       params={"KXHIGHNY": {"bias_f": 2.0}})
    assert shifted["fv_cents"] > base["fv_cents"]


def test_lead_buckets():
    assert [W.lead_bucket(x) for x in (0, 11.9, 12, 30, 48, 100)] == \
        ["0-12h", "0-12h", "12-24h", "24-48h", "48h+", "48h+"]
