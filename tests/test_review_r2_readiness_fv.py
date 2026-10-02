"""readiness fv_calibration: PASS needs paired distinct markets AND distinct
events over the thresholds and the engine's own passing verdict; the
scored-market count with a better paired Brier on a handful of samples is
not enough (the reviewer's false pass)."""
from mm.unattended import fv_calib as C
from tests.test_ops_readiness_report import _run, _status, _write_fixture


def _cal(**over):
    cal = {"verdict": "model_better_than_book", "scored_markets": 260, "paired_markets": 240,
           "overall": {"paired_n": 400, "paired_brier_model": 0.18, "paired_brier_book": 0.21},
           "events": {"paired_events": 50, "overall": {"paired_n": 120, "paired_rps_model": 0.05,
                                                        "paired_rps_book": 0.07}}}
    cal.update(over)
    return cal


def _grade(tmp_path, name, cal, *extra):
    return _run(_write_fixture(tmp_path / name, status=_status(fv_calibration=cal)), *extra)[1]["fv_calibration"]


def test_reviewer_false_pass_is_insufficient(tmp_path):
    cal = {"verdict": "insufficient_data", "scored_markets": 250,
           "overall": {"paired_n": 3, "paired_brier_model": 0.10, "paired_brier_book": 0.20}}
    assert _grade(tmp_path, "a", cal)["status"] == "INSUFFICIENT"


def test_pass_needs_markets_events_and_engine_verdict(tmp_path):
    assert _grade(tmp_path, "ok", _cal())["status"] == "PASS"
    assert _grade(tmp_path, "few_markets", _cal(paired_markets=150))["status"] == "INSUFFICIENT"
    few_ev = _cal(events={"paired_events": 39, "overall": {"paired_rps_model": 0.05, "paired_rps_book": 0.07}})
    assert _grade(tmp_path, "few_events", few_ev)["status"] == "INSUFFICIENT"
    assert _grade(tmp_path, "engine_insuff", _cal(verdict="insufficient_data"))["status"] == "INSUFFICIENT"
    assert _grade(tmp_path, "engine_fail", _cal(verdict="book_better_or_equal"))["status"] == "FAIL"
    worse = _cal(overall={"paired_n": 400, "paired_brier_model": 0.22, "paired_brier_book": 0.21})
    assert _grade(tmp_path, "brier", worse)["status"] == "FAIL"
    assert _grade(tmp_path, "thr", _cal(), "--min-fv-events", "60")["status"] == "INSUFFICIENT"
    assert _grade(tmp_path, "nover", {k: v for k, v in _cal().items() if k != "verdict"})["status"] == \
        "INSUFFICIENT"


def test_engine_report_shape_is_read(tmp_path):
    rep = C.FVCalibration().report()
    g = _grade(tmp_path, "engine", rep)
    assert g["status"] == "INSUFFICIENT" and g["value"]["paired_markets"] == 0
    assert g["value"]["paired_events"] == 0 and g["value"]["verdict"] == "insufficient_data"
