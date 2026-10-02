"""tools/readiness_report.py: read-only go-live readiness over fixture files."""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mm.unattended.health import render_daily_summary

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_ops_rr", ROOT / "tools" / "readiness_report.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ATTR = {"spread_capture_usd": 40.0, "adverse_selection_usd": -15.0, "inventory_mtm_usd": -5.0,
        "est_rewards_kalshi_usd": 30.0, "est_rewards_pmus_usd": 5.0, "rebates_usd": 1.0,
        "fees_usd": -6.0}


def _status(**over):
    st = {
        "paper": True, "live_armed": False, "mode": "paper", "data_source": "production-books",
        "book_source_warning": None, "kill": None, "fills_n": 420,
        "venues": {"kalshi": {"fills_n": 400, "synthetic_fills_n": 0},
                   "pmus": {"fills_n": 20, "synthetic_fills_n": 20}},
        "pnl_usd": "50.0",
        "pnl_attribution": dict(ATTR, total_usd=sum(ATTR.values()), label="estimate (paper)"),
        "markout_horizons": {"by_ref": {"mid": {"10m": {
            "all": {"n": 380, "contracts": 3800.0, "mean_cents": -0.2, "usd": -7.6},
            "venue": {"kalshi": {"n": 360, "contracts": 3600.0, "mean_cents": -0.1, "usd": -3.6}},
            "bucket": {}}}}},
        "daily_mtm_pnl_usd": "-3.5",
        "risk_limits": {"daily_loss_usd": 50.0},
    }
    st.update(over)
    return st


def _write_fixture(tmp_path: Path, *, status=None, days=15, data_source="production-books",
                   settled_n=120, wd_alerts=(), engine_alerts=(), health=None, wd_state=None):
    d = tmp_path / "var"
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(_status() if status is None else status))
    sums = d / "summaries"
    sums.mkdir(exist_ok=True)
    for i in range(days):
        day = (NOW - timedelta(days=i)).date().isoformat()
        (sums / f"daily-summary.{day}").write_text(render_daily_summary(
            day=day, fills=30, pnl_usd=3.0, rewards_usd=0.0, data_source=data_source,
            attribution=ATTR))
    (d / "daily-summary").write_text(render_daily_summary(
        day=NOW.date().isoformat(), fills=30, pnl_usd=3.0, rewards_usd=0.0,
        data_source=data_source, attribution=ATTR))
    settled = {f"KX-{i}": {"result": "yes", "ts": NOW.timestamp() - 3600, "source": "ws_lifecycle"}
               for i in range(settled_n)}
    (d / "engine_state.json").write_text(json.dumps({
        "version": 3, "saved_ts": NOW.timestamp(), "settled": settled, "kill": None,
        "fills_by_venue": {"kalshi": 400, "pmus": 20},
        "fills_synthetic_by_venue": {"pmus": 20}}))
    hl = {"ts": NOW.timestamp() - 30, "ok": True, "latched": False, "config_armed": False,
          "engine_seen_live": None} if health is None else health
    (d / "watchdog_health.json").write_text(json.dumps(hl))
    (d / "watchdog_state.json").write_text(json.dumps(wd_state or {"last_check": NOW.timestamp()}))
    with (d / "alerts.log").open("w") as fh:
        fh.write(json.dumps({"ts": NOW.timestamp() - 20 * 86400, "level": "INFO", "key": "reset",
                             "message": "lip-watchdog reset by operator (was: None)"}) + "\n")
        for rec in wd_alerts:
            fh.write(json.dumps(rec) + "\n")
    with (d / "engine-alerts.log").open("w") as fh:
        fh.write(f"{(NOW - timedelta(days=30)).isoformat()}  INFO      lip_unattended  started\n")
        for line in engine_alerts:
            fh.write(line + "\n")
    return d


def _args(d: Path, *extra):
    return ["--status-file", str(d / "status.json"),
            "--summary", str(d / "daily-summary"), "--summary", str(d / "summaries" / "daily-summary.*"),
            "--state", str(d / "engine_state.json"),
            "--watchdog-health", str(d / "watchdog_health.json"),
            "--watchdog-state", str(d / "watchdog_state.json"),
            "--alerts", str(d / "alerts.log"), "--alerts", str(d / "engine-alerts.log"),
            "--now", NOW.isoformat(), *extra]


def _run(d, *extra):
    rep = R.build_report(R.parse_args(_args(d, *extra)))
    return rep, {c["id"]: c for c in rep["criteria"]}


def _snapshot(d: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(d.rglob("*")) if p.is_file()}


def test_all_pass_is_ready_and_touches_nothing(tmp_path, capsys):
    d = _write_fixture(tmp_path)
    before = _snapshot(d)
    rc = R.main(_args(d))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "OVERALL: READY" in out
    assert "explicit human decision" in out and "LIP_LIVE_ACK" in out
    assert "I_ACCEPT_LIVE_RISK" not in out
    assert _snapshot(d) == before


def test_rewards_share_and_ex_rewards_pnl(tmp_path):
    d = _write_fixture(tmp_path)
    rep, c = _run(d)
    pnl = c["pnl_ex_rewards"]
    assert pnl["status"] == "PASS"
    assert pnl["value"]["ex_rewards_usd"] == pytest.approx(40 - 15 - 5 - 6)
    assert pnl["value"]["rewards_usd"] == pytest.approx(36.0)
    assert pnl["value"]["rewards_share_of_total"] == pytest.approx(36.0 / 50.0)


def test_strategy_losing_without_incentives_fails(tmp_path):
    attr = dict(ATTR, spread_capture_usd=5.0)   # ex-rewards = 5-15-5-6 < 0
    st = _status(pnl_attribution=dict(attr, total_usd=sum(attr.values())))
    d = _write_fixture(tmp_path, status=st)
    rep, c = _run(d)
    assert c["pnl_ex_rewards"]["status"] == "FAIL"
    assert rep["overall"] == "NOT READY"
    assert R.main(_args(d)) == 1


def test_missing_status_is_insufficient_never_pass(tmp_path):
    d = _write_fixture(tmp_path)
    (d / "status.json").unlink()
    rep, c = _run(d)
    for cid in ("paper_days", "markout_10m"):
        assert c[cid]["status"] == "INSUFFICIENT"
    # attribution falls back to the engine's own daily summary line
    assert "daily summary" in c["pnl_ex_rewards"]["detail"]
    assert rep["overall"] == "INSUFFICIENT DATA"
    assert R.main(_args(d)) == 2


def test_status_url_unreachable_is_insufficient(tmp_path):
    d = _write_fixture(tmp_path)
    a = [x for x in _args(d) if x not in ("--status-file", str(d / "status.json"))]
    rep = R.build_report(R.parse_args(a + ["--status-url", "http://127.0.0.1:9/status"]))
    assert {c["id"]: c["status"] for c in rep["criteria"]}["paper_days"] == "INSUFFICIENT"


def test_demo_books_or_warning_fails(tmp_path):
    d = _write_fixture(tmp_path, status=_status(book_source_warning="paper engine is reading DEMO books"))
    assert _run(d)[1]["paper_days"]["status"] == "FAIL"
    d2 = _write_fixture(tmp_path / "b", status=_status(data_source="demo-books: results not representative"))
    assert _run(d2)[1]["paper_days"]["status"] == "FAIL"


def test_too_few_production_days(tmp_path):
    d = _write_fixture(tmp_path, days=5)
    c = _run(d)[1]["paper_days"]
    assert c["status"] == "INSUFFICIENT" and c["value"]["production_days"] == 5
    # the live summary file alone is one day
    a = _args(d)
    i = a.index(str(d / "summaries" / "daily-summary.*"))
    del a[i - 1:i + 1]
    rep = R.build_report(R.parse_args(a))
    assert {c["id"]: c for c in rep["criteria"]}["paper_days"]["value"]["production_days"] == 1


def test_demo_days_do_not_count(tmp_path):
    d = _write_fixture(tmp_path, days=15, data_source="demo-books: results not representative")
    c = _run(d)[1]["paper_days"]
    assert c["value"]["production_days"] == 0 and c["status"] == "INSUFFICIENT"


def test_fill_and_settled_counts(tmp_path):
    d = _write_fixture(tmp_path, status=_status(venues={"kalshi": {"fills_n": 120, "synthetic_fills_n": 0}}))
    c = _run(d)[1]
    assert c["kalshi_fills"]["status"] == "FAIL" and c["kalshi_fills"]["value"] == 120
    d2 = _write_fixture(tmp_path / "b", settled_n=40)
    c2 = _run(d2)[1]["settled_positions"]
    # the state file forgets settled rows after 7 days: a low count is a lower bound
    assert c2["status"] == "INSUFFICIENT" and c2["value"] == 40
    assert _run(_write_fixture(tmp_path / "c"))[1]["settled_positions"]["status"] == "PASS"


def test_markout_floor(tmp_path):
    mh = {"by_ref": {"mid": {"10m": {"all": {"n": 380, "mean_cents": -0.9}, "venue": {}, "bucket": {}}}}}
    d = _write_fixture(tmp_path, status=_status(markout_horizons=mh))
    assert _run(d)[1]["markout_10m"]["status"] == "FAIL"
    assert _run(d, "--markout-floor-cents", "-1.0")[1]["markout_10m"]["status"] == "PASS"
    mh_small = {"by_ref": {"mid": {"10m": {"all": {"n": 12, "mean_cents": 2.0}}}}}
    d2 = _write_fixture(tmp_path / "b", status=_status(markout_horizons=mh_small))
    assert _run(d2)[1]["markout_10m"]["status"] == "INSUFFICIENT"


def test_watchdog_trip_in_last_7_days_fails_unless_operator_test(tmp_path):
    trip = {"ts": NOW.timestamp() - 2 * 86400, "level": "TRIP", "key": "trip",
            "message": "lip-watchdog TRIPPED: heartbeat_stale:400s"}
    d = _write_fixture(tmp_path, wd_alerts=[trip])
    c = _run(d)[1]["no_kills"]
    assert c["status"] == "FAIL" and "heartbeat_stale" in json.dumps(c["value"])
    test_trip = dict(trip, message="lip-watchdog TRIPPED: external kill file (operator test)")
    d2 = _write_fixture(tmp_path / "b", wd_alerts=[test_trip])
    assert _run(d2)[1]["no_kills"]["status"] == "PASS"
    old = dict(trip, ts=NOW.timestamp() - 10 * 86400)
    d3 = _write_fixture(tmp_path / "c", wd_alerts=[old])
    assert _run(d3)[1]["no_kills"]["status"] == "PASS"


def test_engine_kill_latch_alert_fails(tmp_path):
    line = f"{(NOW - timedelta(days=1)).isoformat()}  CRITICAL  lip_unattended  engine kill latched: inventory cap"
    d = _write_fixture(tmp_path, engine_alerts=[line])
    assert _run(d)[1]["no_kills"]["status"] == "FAIL"


def test_current_latches_fail(tmp_path):
    d = _write_fixture(tmp_path, status=_status(kill={"reason": "clock_skew"}))
    assert _run(d)[1]["no_kills"]["status"] == "FAIL"
    d2 = _write_fixture(tmp_path / "b", health={"ts": NOW.timestamp(), "latched": True, "ok": False})
    assert _run(d2)[1]["no_kills"]["status"] == "FAIL"
    d3 = _write_fixture(tmp_path / "c", wd_state={"reset_at": NOW.timestamp() - 86400,
                                                  "reset_prev_reasons": ["daily_loss:-60<-50"]})
    assert _run(d3)[1]["no_kills"]["status"] == "FAIL"


def test_stale_or_missing_watchdog_health_is_insufficient(tmp_path):
    d = _write_fixture(tmp_path, health={"ts": NOW.timestamp() - 7200, "latched": False})
    assert _run(d)[1]["no_kills"]["status"] == "INSUFFICIENT"
    (d / "watchdog_health.json").unlink()
    assert _run(d)[1]["no_kills"]["status"] == "INSUFFICIENT"


def test_daily_loss(tmp_path):
    line = f"{(NOW - timedelta(days=12)).isoformat()}  CRITICAL  lip_unattended  engine kill latched: daily_loss -51 <= -50"
    d = _write_fixture(tmp_path, engine_alerts=[line])
    c = _run(d)[1]
    assert c["daily_loss"]["status"] == "FAIL"
    assert c["no_kills"]["status"] == "PASS"   # older than 7 days
    wd = {"ts": NOW.timestamp() - 9 * 86400, "level": "TRIP", "key": "trip",
          "message": "lip-watchdog TRIPPED: daily_loss:-55.00<-50.00"}
    assert _run(_write_fixture(tmp_path / "b", wd_alerts=[wd]))[1]["daily_loss"]["status"] == "FAIL"
    d3 = _write_fixture(tmp_path / "c", status=_status(daily_mtm_pnl_usd="-50.0"))
    assert _run(d3)[1]["daily_loss"]["status"] == "FAIL"
    d4 = _write_fixture(tmp_path / "d")
    for p in (d4 / "alerts.log", d4 / "engine-alerts.log"):
        p.unlink()
    assert _run(d4)[1]["daily_loss"]["status"] == "INSUFFICIENT"


def test_fv_calibration(tmp_path):
    # Detailed grading (paired markets AND events, the engine's verdict) is in
    # test_review_r2_readiness_fv; formats without them are INSUFFICIENT.
    d = _write_fixture(tmp_path)
    c = _run(d)[1]["fv_calibration"]
    assert c["status"] == "N/A"
    old_shape = _status(fv_calibration={"n_settled": 250, "model_brier": 0.18, "book_brier": 0.21})
    assert _run(_write_fixture(tmp_path / "a", status=old_shape))[1]["fv_calibration"]["status"] == "INSUFFICIENT"
    junk = _status(fv_calibration={"something": 1})
    assert _run(_write_fixture(tmp_path / "e", status=junk))[1]["fv_calibration"]["status"] == "INSUFFICIENT"


def test_json_output(tmp_path, capsys):
    d = _write_fixture(tmp_path)
    assert R.main(_args(d, "--json")) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["overall"] == "READY" and rep["read_only"] is True
    assert {c["id"] for c in rep["criteria"]} >= {
        "paper_days", "kalshi_fills", "settled_positions", "pnl_ex_rewards", "markout_10m",
        "no_kills", "fv_calibration", "daily_loss"}


def test_tool_source_never_writes_or_arms():
    src = (ROOT / "tools" / "readiness_report.py").read_text()
    import re
    assert not re.search(r"(?<![a-z])open\(", src)
    for forbidden in ("write_text", ".unlink(", "os.replace", "os.environ[",
                      "enable_kalshi_maker_only_enforcement", "MAKER_ONLY_ENFORCEMENT_VERIFIED =",
                      "method=\"POST\"", "urlopen(req, data"):
        assert forbidden not in src, forbidden
