"""tools/fit_fv_weather.py: maximum-likelihood bias / inflation per station
from the engine's settled calibration samples; refuses stations with too
few settled events; the written file loads as LIP_FV_WX_PARAMS_FILE."""
from __future__ import annotations

import importlib.util
import json
import math
import random
from pathlib import Path

import pytest

from mm.unattended import fv_weather as W

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_r2_fit", ROOT / "tools" / "fit_fv_weather.py")
FIT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FIT)


def _synth(path: Path, station: str, n_events: int, *, bias=1.5, inflation=1.0, kernel=1.75, seed=3):
    rnd = random.Random(seed)
    with path.open("a") as fh:
        for e in range(n_events):
            mu, sd = rnd.uniform(55, 90), rnd.uniform(0.3, 4.0)
            true = rnd.gauss(mu + bias, math.sqrt((inflation * sd) ** 2 + kernel ** 2))
            cli = math.floor(true + 0.5)
            c = round(mu)
            ranges = [[None, c - 3], [c - 2, c - 1], [c, c + 1], [c + 2, c + 3], [c + 4, None]]
            event = f"{station}-E{e}"
            for lo, hi in ranges:
                y = int((lo is None or cli >= lo) and (hi is None or cli <= hi))
                fh.write(json.dumps({"market": f"{event}-{lo}-{hi}", "event": event, "station": station,
                                     "lead_bucket": "24-48h", "y": y, "range": [lo, hi], "fv": 20.0,
                                     "mid": 25.0, "ens": {"mean": mu, "sd": sd, "n": 82, "floor_f": None,
                                                          "slip": 0.1, "after_window": False}}) + "\n")


def test_recovers_the_bias_and_writes_a_loadable_file(tmp_path, capsys):
    samples = tmp_path / "s.jsonl"
    _synth(samples, "KXHIGHNY", 150)
    _synth(samples, "KXHIGHCHI", 10, seed=4)           # too few events: not fitted
    out = tmp_path / "params.json"
    rc = FIT.main(["--samples", str(samples), "--out", str(out)])
    text = capsys.readouterr().out
    assert rc == 0, text
    params = W.load_params(str(out))
    assert set(params) == {"KXHIGHNY"}
    ny = params["KXHIGHNY"]
    assert ny["bias_f"] == pytest.approx(1.5, abs=0.6)
    assert 0.5 <= ny["inflation"] <= 1.8 and ny["kernel_sd_f"] == 1.75
    assert "KXHIGHCHI: samples=50 events=10 -> skipped: 10 settled events < 30" in text
    assert "KXHIGHNY: samples=750 events=150 -> fitted" in text


def test_refuses_to_write_without_enough_events(tmp_path, capsys):
    samples = tmp_path / "s.jsonl"
    _synth(samples, "KXHIGHNY", 29)
    out = tmp_path / "params.json"
    assert FIT.main(["--samples", str(samples), "--out", str(out)]) == 2
    assert not out.exists()
    assert FIT.main(["--samples", str(samples), "--out", str(out), "--min-events", "20"]) == 0
    assert out.exists()


def test_skips_after_window_and_bad_rows(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("not json\n" + json.dumps({"station": "KXHIGHNY", "y": 1, "range": [70, 71],
                                             "ens": {"mean": 70, "sd": 0, "after_window": True}}) + "\n")
    rows, errors = FIT.read_samples([str(p), str(tmp_path / "missing.jsonl")])
    assert rows == [] and len(errors) == 2


def test_default_path_follows_the_engine_env(tmp_path, monkeypatch, capsys):
    samples = tmp_path / "engine.jsonl"
    _synth(samples, "KXHIGHNY", 31)
    monkeypatch.setenv("LIP_FV_CALIB_SAMPLES_FILE", str(samples))
    assert FIT.main(["--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["inputs"]["files"] == [str(samples)] and "KXHIGHNY" in rep["params"]
