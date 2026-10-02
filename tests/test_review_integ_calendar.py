"""Phase 4: scheduled-event calendar. Quotes come off (and stay off) on
matching markets inside an event's pre/post window."""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from config.event_calendar import ENV, EventCalendar
from tests.test_review_integ_loop import (  # noqa: F401  (autouse env fixture)
    K, T0, _env, newloop, program, snap,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "deploy/apex/event_calendar.example.json"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _write(tmp_path, events, **extra):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"events": events, **extra}))
    return path


def test_no_file_means_no_pulls_and_one_log_line(caplog):
    caplog.set_level(logging.INFO, logger="lip.calendar")
    cal = EventCalendar(None)
    for i in range(5):
        assert cal.check("KXCPI", K, T0 + i) == ""
    assert sum("no scheduled-event calendar" in r.getMessage() for r in caplog.records) == 1
    assert cal.summary()["configured"] is False


def test_window_and_prefix_matching(tmp_path):
    at = T0 + 3600
    path = _write(tmp_path, [{"name": "test cpi", "series_prefixes": ["KXCPI"], "at": _iso(at),
                              "pre_minutes": 30, "post_minutes": 15}])
    cal = EventCalendar(str(path))
    assert cal.check("KXCPI", K, at - 1801) == ""
    assert cal.check("KXCPI", K, at - 1800) == "scheduled_event"
    assert cal.check("KXCPIYOY", "KXCPIYOY-26OCT-T3", at) == "scheduled_event"   # prefix match
    assert cal.check("kxcpi", "kxcpi-x", at) == "scheduled_event"                # case-insensitive
    assert cal.check("KXFED", "KXFED-26OCT-T4", at) == ""
    assert cal.check("KXCPI", K, at + 900) == "scheduled_event"
    assert cal.check("KXCPI", K, at + 901) == ""
    assert cal.active(at)[0]["name"] == "test cpi"


def test_defaults_apply_to_entries_without_windows(tmp_path):
    at = T0 + 3600
    path = _write(tmp_path, [{"name": "x", "series_prefixes": ["KXFED"], "at": _iso(at)}],
                  defaults={"pre_minutes": 10, "post_minutes": 5})
    cal = EventCalendar(str(path))
    assert cal.check("KXFED", "KXFED-1", at - 601) == ""
    assert cal.check("KXFED", "KXFED-1", at - 600) == "scheduled_event"
    assert cal.check("KXFED", "KXFED-1", at + 301) == ""


@pytest.mark.parametrize("body", [
    "{not json",
    json.dumps({"events": [{"name": "x", "series_prefixes": ["KXCPI"], "at": "2026-10-01T12:00:00"}]}),
    json.dumps({"events": [{"name": "x", "series_prefixes": [], "at": "2026-10-01T12:00:00Z"}]}),
    json.dumps({"events": [{"name": "x", "series_prefixes": ["KXCPI"], "at": "2026-10-01T12:00:00Z",
                            "pre_minutes": -5}]}),
    json.dumps({"events": "nope"}),
])
def test_a_configured_but_invalid_file_blocks_every_market(tmp_path, body):
    path = tmp_path / "cal.json"
    path.write_text(body)
    cal = EventCalendar(str(path))
    assert cal.check("KXFED", "KXFED-1", T0) == "scheduled_event_calendar_error"
    assert cal.summary()["error"]


def test_a_configured_file_that_disappears_fails_closed(tmp_path):
    cal = EventCalendar(str(tmp_path / "missing.json"))
    assert cal.check("KXFED", "KXFED-1", T0) == "scheduled_event_calendar_error"


def test_file_is_reloaded_when_it_changes(tmp_path):
    at = T0 + 3600
    path = _write(tmp_path, [])
    clock = {"t": 1000.0}
    cal = EventCalendar(str(path), clock=lambda: clock["t"], reload_every_s=60)
    assert cal.check("KXCPI", K, at) == ""
    path.write_text(json.dumps({"events": [{"name": "late add", "series_prefixes": ["KXCPI"],
                                            "at": _iso(at), "pre_minutes": 5, "post_minutes": 5}]}))
    os.utime(path, (2_000_000_000, 2_000_000_000))
    assert cal.check("KXCPI", K, at) == ""          # not re-read inside reload_every_s
    clock["t"] += 61
    assert cal.check("KXCPI", K, at) == "scheduled_event"


def test_example_file_is_labelled_and_loads(tmp_path):
    raw = json.loads(EXAMPLE.read_text())
    assert "EXAMPLE" in raw["_comment"] and "operator" in raw["_comment"]
    assert len(raw["events"]) >= 3
    assert all("EXAMPLE" in e["name"] for e in raw["events"])
    prefixes = {p for e in raw["events"] for p in e["series_prefixes"]}
    assert {"KXCPI", "KXFED", "KXPAYROLLS"} <= prefixes
    cal = EventCalendar(str(EXAMPLE))
    assert cal.summary()["error"] is None and cal.summary()["events_n"] == len(raw["events"])
    # example dates are placeholders far in the future: nothing is active now
    assert cal.active(T0) == []


def test_yaml_file_loads_when_pyyaml_is_installed(tmp_path):
    yaml = pytest.importorskip("yaml")
    path = tmp_path / "cal.yaml"
    path.write_text(yaml.safe_dump({"events": [{"name": "y", "series_prefixes": ["KXCPI"],
                                                "at": _iso(T0), "pre_minutes": 1, "post_minutes": 1}]}))
    assert EventCalendar(str(path)).check("KXCPI", K, T0) == "scheduled_event"


# ------------------------------------------------------------------ RunLoop wiring
def _quoting(monkeypatch, path):
    monkeypatch.setenv(ENV, str(path))
    lp = newloop(select_every=10 ** 9)   # only the first selection runs on its own
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    return lp


def test_loop_pulls_and_refuses_quotes_inside_the_window(tmp_path, monkeypatch):
    at = T0 + 3600
    path = _write(tmp_path, [{"name": "cpi", "series_prefixes": ["KXCPI"], "at": _iso(at),
                              "pre_minutes": 30, "post_minutes": 15}])
    lp = _quoting(monkeypatch, path)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert K in lp.resting
    lp.on_frame({"type": "clock", "ts": at - 1700})
    assert K not in lp.resting and lp.pulls.get("scheduled_event") == 1
    assert any(c["reason"] == "scheduled_event" for c in lp.cancels)
    # a re-selection inside the window refuses the market
    lp._select(at - 1600)
    assert K not in lp.resting and (K, "scheduled_event") in lp.policy_skips
    # and the direct quote path refuses it too
    assert lp._quote(K, 40, 55, 100.0, at - 1500) is False
    st = lp.live_snapshot()
    assert st["event_calendar"]["configured"] is True
    assert [e["name"] for e in st["event_calendar"]["active"]] == ["cpi"]
    # after the window the market is quoted again
    lp._select(at + 901)
    assert K in lp.resting


def test_loop_without_calendar_quotes_normally(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert K in lp.resting
    assert lp.live_snapshot()["event_calendar"]["configured"] is False


def test_loop_with_a_broken_calendar_quotes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "cal.json"
    path.write_text("{broken")
    lp = _quoting(monkeypatch, path)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert lp.resting == {}
    assert (K, "scheduled_event_calendar_error") in lp.policy_skips
