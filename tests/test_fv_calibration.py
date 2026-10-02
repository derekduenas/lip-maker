"""Model fair value calibration: recording, settlement scoring (Brier and log
loss vs the book mid), per-station / lead-bucket aggregates, /status and
engine-state persistence."""
import json
import math
import time

import pytest

from mm.unattended import fv_calib as C
from mm.unattended import fv_weather as W
from mm.unattended import loop as L
from tests.test_fv_quote import MKT, _FV, _on, _prog, _env  # noqa: F401  (autouse env fixture)
from tests.test_patch15 import T0, _book


def test_scores_and_aggregates():
    c = C.FVCalibration()
    # 40h ahead: model 80c, book 60c; 6h ahead: model 90c, book 70c
    assert c.record(MKT, "KXHIGHNY", T0, 80.0, 0.8, 40.0, 60.0, fv_ts=1.0)
    assert not c.record(MKT, "KXHIGHNY", T0 + 1, 80.0, 0.8, 40.0, 60.0, fv_ts=1.0)   # same value
    assert not c.record(MKT, "KXHIGHNY", T0 + 300, 82.0, 0.8, 39.9, 61.0, fv_ts=2.0)  # same bucket: first kept
    assert c.record(MKT, "KXHIGHNY", T0 + 34 * 3600, 90.0, 0.95, 6.0, 70.0, fv_ts=3.0)
    assert c.on_settle(MKT, "yes") == 2
    assert c.on_settle(MKT, "yes") == 0          # scored once
    rep = c.report()
    o = rep["overall"]
    assert o["n"] == 2 and o["paired_n"] == 2
    assert o["brier"] == pytest.approx(((0.2 ** 2) + (0.1 ** 2)) / 2, abs=1e-5)
    assert o["paired_brier_book"] == pytest.approx(((0.4 ** 2) + (0.3 ** 2)) / 2, abs=1e-5)
    assert o["logloss"] == pytest.approx((-math.log(0.8) - math.log(0.9)) / 2, abs=1e-5)
    assert o["skill_vs_book"] > 0
    assert set(rep["by_lead"]) == {"24-48h", "0-12h"} and set(rep["by_station"]) == {"KXHIGHNY"}
    assert rep["verdict"] == "insufficient_data"


def test_unpaired_samples_and_no_result_and_verdict(monkeypatch):
    c = C.FVCalibration()
    c.record("A", "KXHIGHCHI", T0, 30.0, 0.7, 20.0, None, fv_ts=1.0)
    c.on_settle("A", "no")
    o = c.report()["overall"]
    assert o["n"] == 1 and o["paired_n"] == 0 and o["skill_vs_book"] is None
    assert c.on_settle("missing", "yes") == 0
    monkeypatch.setenv("LIP_FV_CALIB_MIN_N", "1")
    c.record("B", "KXHIGHCHI", T0, 10.0, 0.7, 20.0, 50.0, fv_ts=1.0)
    c.on_settle("B", "no")
    assert c.report()["verdict"] == "model_better_than_book"
    c.record("D", "KXHIGHCHI", T0, 99.0, 0.7, 20.0, 50.0, fv_ts=1.0)
    c.on_settle("D", "no")
    assert c.report()["verdict"] == "book_better_or_equal"


def test_state_roundtrip_and_validation():
    c = C.FVCalibration()
    c.record(MKT, "KXHIGHNY", T0, 80.0, 0.8, 40.0, 60.0, fv_ts=1.0)
    c.record("X", "KXHIGHNY", T0, 20.0, 0.8, 40.0, 30.0, fv_ts=1.0)
    c.on_settle("X", "no")
    blob = json.loads(json.dumps(c.state()))
    d = C.FVCalibration()
    d.load_state(blob)
    assert d.report()["overall"] == c.report()["overall"] and MKT in d.pending
    with pytest.raises(ValueError):
        d.load_state({"agg": {"nobar": {}}})
    with pytest.raises((KeyError, TypeError, ValueError)):
        d.load_state({"pending": {"m": {"samples": {}}}})


def test_prune_and_bounded_pending():
    c = C.FVCalibration(max_pending=2)
    for i, m in enumerate(("a", "b", "c")):
        c.record(m, "KXHIGHNY", T0 + i, 50.0, 0.8, 30.0, 50.0, fv_ts=1.0)
    assert set(c.pending) == {"b", "c"} and c.dropped == 1
    assert c.prune(T0 + 11 * 86400) == 2 and not c.pending


# ---------------------------------------------------------------- loop wiring
class _WxFV(_FV):
    def get(self, market, now=None):
        row = super().get(market, now)
        if row is not None:
            row.update(lead_h=30.0, ts=self.ts)
        return row

    ts = 1000.0


def _loop(monkeypatch):
    _on(monkeypatch)
    loop = L.RunLoop(mode="paper", bankroll=5000)
    loop.fv = _WxFV(62.0)
    _prog(loop)
    _book(loop, MKT, [(30, 2000)], [(60, 2000)], T0)
    return loop


def test_loop_records_with_book_mid_and_scores_on_settle(monkeypatch):
    loop = _loop(monkeypatch)
    loop._fv_calib_tick(T0 + 1)
    pend = loop.fv_calib.pending[MKT]
    s = pend["samples"]["24-48h"]
    assert pend["station"] == "KXHIGHNY" and s["fv"] == 62.0 and s["mid"] == 35.0 and s["conf"] == 0.9
    loop.fv.ts = 2000.0
    loop._fv_calib_tick(T0 + 2)              # rate limited: nothing new before the interval
    assert len(loop.fv_calib.recent) == 1
    loop._fv_calib_tick(T0 + 100)
    assert len(loop.fv_calib.recent) == 2
    # no position, program still here: settle path scores it
    loop.settle(MKT, "yes")
    rep = loop.live_snapshot()["fv_calibration"]
    assert rep["overall"]["n"] == 1 and rep["overall"]["paired_n"] == 1
    assert rep["by_station"]["KXHIGHNY"]["brier"] == pytest.approx(0.38 ** 2, abs=1e-5)


def test_settle_scores_even_after_program_is_gone(monkeypatch):
    loop = _loop(monkeypatch)
    loop._fv_calib_tick(T0 + 1)
    loop.end_program(MKT)
    loop.settle(MKT, "no")
    assert loop.fv_calib.report()["overall"]["n"] == 1
    assert MKT not in loop.settled             # nothing held: settlement still ignored for P&L


def test_non_model_rows_and_other_families_not_recorded(monkeypatch):
    loop = _loop(monkeypatch)
    loop.fv_calib = C.FVCalibration()     # the book frame already recorded once
    loop.fv.source = "polymarket"
    loop._fv_calib_tick(T0 + 100)
    assert not loop.fv_calib.pending
    loop.fv.source = W.SOURCE
    monkeypatch.setenv("LIP_FV_QUOTE_FAMILIES", "KXRAIN")
    loop._fv_calib_tick(T0 + 200)
    assert not loop.fv_calib.pending


def test_calibration_persists_in_engine_state(monkeypatch, tmp_path):
    loop = _loop(monkeypatch)
    loop._fv_calib_tick(T0 + 1)
    loop.settle(MKT, "yes")
    loop.fv.ts = 5000.0
    _prog(loop, market="KXHIGHNY-26OCT01-T75")
    _book(loop, "KXHIGHNY-26OCT01-T75", [(30, 2000)], [(60, 2000)], T0 + 2)
    loop._fv_calib_tick(T0 + 200)
    path = tmp_path / "state.json"
    loop.attach_state(str(path))
    assert loop.save_state(force=True)
    data = json.loads(path.read_text())
    assert data["fv_calibration"]["agg"] and "KXHIGHNY-26OCT01-T75" in data["fv_calibration"]["pending"]
    fresh = L.RunLoop(mode="paper", bankroll=5000)
    fresh.attach_state(str(path))
    assert fresh.kill is None
    assert fresh.fv_calib.report()["overall"]["n"] == 1
    assert "KXHIGHNY-26OCT01-T75" in fresh.fv_calib.pending


def test_old_state_without_section_loads_and_bad_section_fails_closed(monkeypatch, tmp_path):
    loop = _loop(monkeypatch)
    path = tmp_path / "state.json"
    loop.attach_state(str(path))
    loop.save_state(force=True)
    data = json.loads(path.read_text())
    data.pop("fv_calibration")
    path.write_text(json.dumps(data))
    ok = L.RunLoop(mode="paper", bankroll=5000)
    ok.attach_state(str(path))
    assert ok.kill is None and ok.fv_calib.report()["overall"]["n"] == 0
    data["fv_calibration"] = {"agg": {"bad": 1}}
    path.write_text(json.dumps(data))
    bad = L.RunLoop(mode="paper", bankroll=5000)
    bad.attach_state(str(path))
    assert bad.kill is not None and "state_file_unreadable" in bad.kill["reason"]
