"""Review (2026-10-01): run_paper imported a name that does not exist.

PaperRunner._calibration_for imported ``calibration_for`` from
engine.calibration_ewma, which defines ``calib_for``. The ImportError was
swallowed, so the learned calibration was never read (always 1.0).
"""
from __future__ import annotations

import engine.calibration_ewma as ce
from run_paper import PaperRunner


def test_calibration_for_reads_calib_for(monkeypatch):
    seen = {}

    def fake(key, fallback, **kw):
        seen["args"] = (key, fallback)
        return 0.4

    monkeypatch.setattr(ce, "calib_for", fake)
    assert PaperRunner._calibration_for(None, "KXTEST-1") == 0.4
    assert seen["args"] == ("KXTEST-1", 1.0)


def test_calibration_for_defaults_to_one(monkeypatch):
    monkeypatch.setattr(ce, "calib_for", lambda key, fallback, **kw: fallback)
    assert PaperRunner._calibration_for(None, "KXTEST-1") == 1.0
