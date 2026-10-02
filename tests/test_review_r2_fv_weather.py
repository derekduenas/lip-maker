"""fv_weather review fixes: the observation floor sits at the CLI-rounded
observed max (with a small explicit slip to one degree below), a started
window without an observation prices nothing, conservative unfitted
defaults, documented Open-Meteo model identifiers, and a member-max summary
on every row."""
import json
import math
import statistics
from pathlib import Path

import pytest

from mm.unattended import fv_weather as W
from tests.test_fv_weather import FIX, _Http, _ens, _utc

NOW_DAY = _utc(2026, 10, 1, 20)   # inside the KNYC window of 2026-10-01


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def test_cli_round_converts_celsius_carefully():
    assert W.cli_round_f(24.4 * 9 / 5 + 32) == 76      # 75.92 F
    assert W.cli_round_f(23.6 * 9 / 5 + 32) == 74      # 74.48 F
    assert W.cli_round_f(2.5 * 9 / 5 + 32) == 37       # 36.5 F: half rounds up
    assert W.cli_round_f(75.494) == 75 and W.cli_round_f(-0.5) == 0
    assert W.cli_round_f(75.49999999999) == 76          # float noise of 75.5


def test_reviewer_case_observed_75_92_remaining_members_70():
    # Observed 75.92 F (24.4 C) and every member says 70 F for the rest of
    # the day: the CLI max is 76 (P ~ 0.9) or, by the ASOS->CLI slip, 75.
    obs = json.loads((FIX / "obs_knyc_2026-10-01.json").read_text())
    m = W.WeatherHighModel(_Http(ens=_ens([70.0] * 30), obs=obs))
    lo = m.fv_for("KXHIGHNY-26OCT01-B74.5", {"strike_type": "between", "floor_strike": 74, "cap_strike": 75},
                  now=NOW_DAY)
    hi = m.fv_for("KXHIGHNY-26OCT01-B76.5", {"strike_type": "between", "floor_strike": 76, "cap_strike": 77},
                  now=NOW_DAY)
    assert lo["fv_cents"] == pytest.approx(10.0, abs=0.2)
    assert hi["fv_cents"] == pytest.approx(90.0, abs=0.2)
    assert lo["ens"]["floor_f"] == 76 and lo["obs_max_f"] == 75.9


def test_round_down_case_and_slip_is_configurable(monkeypatch):
    vals = [70.0] * 10
    # observed 75.3 F -> CLI 75: 0.9 at 75, 0.1 at 74, nothing at 76+
    assert W.bucket_prob(vals, 75, 75, sd=1.75, floor_f=75, floor_slip=0.1) == pytest.approx(0.9, abs=2e-3)
    assert W.bucket_prob(vals, 74, 74, sd=1.75, floor_f=75, floor_slip=0.1) == pytest.approx(0.1, abs=1e-3)
    assert W.bucket_prob(vals, 76, None, sd=1.75, floor_f=75, floor_slip=0.1) < 0.002
    assert W.bucket_prob(vals, None, 73, sd=1.75, floor_f=75, floor_slip=0.1) == 0.0
    edges = [(None, 71), (72, 73), (74, 74), (75, 75), (76, 77), (78, None)]
    assert sum(W.bucket_prob(vals, a, b, sd=1.75, floor_f=75, floor_slip=0.1) for a, b in edges) == \
        pytest.approx(1.0, abs=1e-12)
    obs = {"features": [{"properties": {"timestamp": "2026-10-01T18:00:00+00:00",
                                        "temperature": {"value": 75.3, "unitCode": "wmoUnit:degF"}}}]}
    m = W.WeatherHighModel(_Http(ens=_ens([70.0] * 30), obs=obs))
    row = m.fv_for("KXHIGHNY-26OCT01-B74.5", {"strike_type": "between", "floor_strike": 74, "cap_strike": 75},
                   now=NOW_DAY)
    assert row["fv_cents"] == 99.0 and row["ens"]["floor_f"] == 75
    monkeypatch.setenv("LIP_FV_WX_OBS_SLIP", "0")
    assert W.bucket_prob(vals, 74, 74, sd=1.75, floor_f=75, floor_slip=W.obs_slip()) == 0.0
    monkeypatch.setenv("LIP_FV_WX_OBS_SLIP", "0.9")      # clamped: never more than half
    assert W.obs_slip() == 0.5


def test_floor_must_be_a_whole_degree():
    with pytest.raises(ValueError):
        W.bucket_prob([70.0], 74, 75, floor_f=74.6)


def test_forecast_above_the_floor_is_unchanged():
    vals = [80.0, 81.0, 82.0]
    plain = W.bucket_prob(vals, 80, 81, sd=1.75, inflation=1.4)
    assert W.bucket_prob(vals, 80, 81, sd=1.75, inflation=1.4, floor_f=70, floor_slip=0.1) == \
        pytest.approx(plain, abs=1e-9)


@pytest.mark.parametrize("obs", [{"features": []}, None])
def test_started_window_without_observation_prices_nothing(obs):
    http = _Http(ens=_ens([70.0] * 30), obs=obs, raise_obs=obs is None)
    m = W.WeatherHighModel(http)
    assert m.fv_for("KXHIGHNY-26OCT01-T71", {"strike_type": "less", "cap_strike": 71}, now=NOW_DAY) is None
    assert m.stats["no_obs"] == 1


def test_conservative_unfitted_defaults_and_documented_models(monkeypatch):
    monkeypatch.delenv("LIP_FV_WX_KERNEL_SD", raising=False)
    monkeypatch.delenv("LIP_FV_WX_MODELS", raising=False)
    assert W.DEFAULT_PARAMS == {"bias_f": 0.0, "inflation": 1.4}
    assert W.DEFAULT_KERNEL_SD_F == 1.75
    # identifiers listed on https://open-meteo.com/en/docs/ensemble-api
    assert W.DEFAULT_MODELS == "gfs025,ecmwf_ifs025"
    http = _Http(ens=_ens([72.5] * 30))
    row = W.WeatherHighModel(http).fv_for("KXHIGHNY-26OCT01-B72.5",
                                           {"strike_type": "between", "floor_strike": 72, "cap_strike": 73},
                                           now=_utc(2026, 9, 30, 15))
    assert http.calls[0][1]["models"] == "gfs025,ecmwf_ifs025"
    want = _phi(1.0 / 1.75) - _phi(-1.0 / 1.75)
    assert row["fv_cents"] == pytest.approx(round(want * 100, 2), abs=0.01)
    assert row["params"] == {"bias_f": 0.0, "inflation": 1.4, "kernel_sd_f": 1.75}


def test_row_carries_member_max_summary():
    members = [70.0, 71.0, 72.0, 73.0, 74.0] * 6
    http = _Http(ens=_ens(members))
    row = W.WeatherHighModel(http).fv_for("KXHIGHNY-26OCT01-B72.5",
                                           {"strike_type": "between", "floor_strike": 72, "cap_strike": 73},
                                           now=_utc(2026, 9, 30, 15))
    ens = row["ens"]
    assert ens["n"] == 30 and ens["mean"] == pytest.approx(72.0)
    assert ens["sd"] == pytest.approx(statistics.pstdev(members), abs=1e-3)
    assert ens["floor_f"] is None and ens["after_window"] is False
