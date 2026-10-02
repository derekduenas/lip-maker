"""daily-summary.history.jsonl: one line per UTC day (final summary of the
day), written on day roll, periodically and on shutdown, bounded; the
readiness report counts production-books days from it."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from mm.unattended import service as S

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_next_rr4", ROOT / "tools" / "readiness_report.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

PROD = "production-books"


def _args(tmp_path):
    return argparse.Namespace(heartbeat=str(tmp_path / "hb"), summary=str(tmp_path / "daily-summary"),
                              report="", status_port=0)


def _report(day, fills, src=PROD):
    return {"day": day, "fills_n": fills, "pnl_usd": "1.5", "rewards_usd": "0", "premium_paid_usd": "2",
            "data_source": src, "pnl_attribution": {"spread_capture_usd": 0.5, "adverse_selection_usd": -0.1,
                                                    "inventory_mtm_usd": 0.0, "fees_usd": -0.01,
                                                    "total_usd": 1.5}}


def _lines(path):
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


@pytest.fixture(autouse=True)
def _no_periodic(monkeypatch):
    monkeypatch.setenv("LIP_SUMMARY_HISTORY_EVERY_S", "0")   # day roll and shutdown only
    S._SUMMARY_HISTORIES.clear()
    yield
    S._SUMMARY_HISTORIES.clear()


def test_day_roll_appends_the_final_summary_of_the_day(tmp_path):
    a = _args(tmp_path)
    hist = tmp_path / "daily-summary.history.jsonl"
    S._write_run_outputs(a, _report("2026-10-01", 3))
    S._write_run_outputs(a, _report("2026-10-01", 7))
    assert not hist.exists()                       # nothing final yet
    S._write_run_outputs(a, _report("2026-10-02", 8))
    rows = _lines(hist)
    assert [r["day"] for r in rows] == ["2026-10-01"]
    assert rows[0]["fills"] == 7 and rows[0]["data_source"] == PROD and rows[0]["data_sources"] == [PROD]
    assert rows[0]["attribution"]["spread_capture_usd"] == 0.5
    # the summary file itself still holds only the current day
    assert (tmp_path / "daily-summary").read_text().startswith("daily summary 2026-10-02")
    # clean shutdown writes the current day; a second flush does not duplicate it
    S.flush_summary_histories()
    S.flush_summary_histories()
    assert [r["day"] for r in _lines(hist)] == ["2026-10-01", "2026-10-02"]


def test_restart_same_day_replaces_the_line_and_keeps_every_source(tmp_path):
    a = _args(tmp_path)
    hist = tmp_path / "daily-summary.history.jsonl"
    S._write_run_outputs(a, _report("2026-10-01", 3, src="demo-books: results not representative"))
    S.flush_summary_histories()
    S._SUMMARY_HISTORIES.clear()                   # a new process
    S._write_run_outputs(a, _report("2026-10-01", 9))
    S.flush_summary_histories()
    rows = _lines(hist)
    assert len(rows) == 1 and rows[0]["fills"] == 9
    assert rows[0]["data_sources"] == ["demo-books: results not representative", PROD]


def test_history_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("LIP_SUMMARY_HISTORY_MAX", "5")
    a = _args(tmp_path)
    for d in range(1, 10):
        S._write_run_outputs(a, _report(f"2026-10-{d:02d}", d))
    S.flush_summary_histories()
    rows = _lines(tmp_path / "daily-summary.history.jsonl")
    assert [r["day"] for r in rows] == [f"2026-10-{d:02d}" for d in range(5, 10)]


def test_default_bound_is_400_lines(tmp_path):
    hist = tmp_path / "daily-summary.history.jsonl"
    hist.write_text("".join(json.dumps({"day": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", "fills": i}) + "\n"
                            for i in range(330)) + "not json\n"
                    + "".join(json.dumps({"day": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"}) + "\n"
                              for i in range(100)))
    S._write_run_outputs(_args(tmp_path), _report("2026-10-01", 1))
    S.flush_summary_histories()
    rows = _lines(hist)
    assert len(rows) == 400 and rows[-1]["day"] == "2026-10-01"


def test_periodic_checkpoint(tmp_path):
    clock = {"t": 1000.0}
    h = S.SummaryHistory(str(tmp_path / "h.jsonl"), every_s=600.0, clock=lambda: clock["t"])
    h.note({"day": "2026-10-01", "data_source": PROD})
    assert not (tmp_path / "h.jsonl").exists()
    clock["t"] += 601
    h.note({"day": "2026-10-01", "data_source": PROD, "fills": 4})
    assert _lines(tmp_path / "h.jsonl")[0]["fills"] == 4


def test_unwritable_history_never_breaks_the_summary(tmp_path):
    a = _args(tmp_path)
    (tmp_path / "daily-summary.history.jsonl").mkdir()     # a directory where the file should be
    S._write_run_outputs(a, _report("2026-10-01", 1))
    S._write_run_outputs(a, _report("2026-10-02", 2))
    S.flush_summary_histories()
    assert (tmp_path / "daily-summary").read_text().startswith("daily summary 2026-10-02")


def test_replay_run_writes_history_on_exit(tmp_path):
    from tests.test_unattended_run import _stream
    path = tmp_path / "stream.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in _stream()) + "\n", encoding="utf-8")
    summary = tmp_path / "summary.txt"
    assert S.main(["--run", "--replay", str(path), "--once", "--summary", str(summary),
                   "--heartbeat", str(tmp_path / "hb"), "--cancel-log", str(tmp_path / "cancel")]) == 0
    rows = _lines(tmp_path / "summary.txt.history.jsonl")
    assert len(rows) == 1 and rows[0]["day"] == summary.read_text().split("\n")[0].split()[-1]


def _ra(tmp_path, **kw):
    a = R.parse_args(["--status-file", str(tmp_path / "status.json"),
                      "--summary", str(tmp_path / "daily-summary"),
                      "--state", str(tmp_path / "none"), "--watchdog-health", str(tmp_path / "none"),
                      "--watchdog-state", str(tmp_path / "none"), "--alerts", str(tmp_path / "none")])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_readiness_counts_days_from_the_history(tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"paper": True, "data_source": PROD}))
    a = _args(tmp_path)
    for d in range(1, 16):
        S._write_run_outputs(a, _report(f"2026-10-{d:02d}", d))
    S.flush_summary_histories()
    rep = R.build_report(_ra(tmp_path))
    crit = {c["id"]: c for c in rep["criteria"]}["paper_days"]
    assert crit["status"] == R.PASS and crit["value"]["production_days"] == 15
    assert str(tmp_path / "daily-summary.history.jsonl") in rep["inputs"]["summaries"]


def test_readiness_mixed_source_day_does_not_count(tmp_path):
    hist = tmp_path / "daily-summary.history.jsonl"
    hist.write_text(json.dumps({"day": "2026-10-01", "data_source": PROD,
                                "data_sources": ["demo-books: results not representative", PROD]}) + "\n"
                    + json.dumps({"day": "2026-10-02", "data_source": PROD, "data_sources": [PROD]}) + "\n")
    (tmp_path / "status.json").write_text(json.dumps({"paper": True, "data_source": PROD}))
    rep = R.build_report(_ra(tmp_path))
    crit = {c["id"]: c for c in rep["criteria"]}["paper_days"]
    assert crit["value"]["production_days"] == 1 and crit["value"]["non_production_days"] == ["2026-10-01"]
