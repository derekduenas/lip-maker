"""The served /status carries every live RunLoop status key (fv_calibration,
positions, markout_horizons, pnl_attribution, event_calendar, settled
counters), checked through the real HTTP handler."""
from __future__ import annotations

import json
import threading
import urllib.request

from mm.status_page import StatusPage
from mm.unattended import loop as L

T0 = 1_790_000_000.0
NEW_KEYS = ("fv_calibration", "positions", "markout_horizons", "pnl_attribution", "event_calendar",
            "settled_positions_n", "settled_positions_by_venue", "settled_total_usd",
            "settled_positions_lower_bound")


def _get(page) -> dict:
    t = threading.Thread(target=page.serve_one, daemon=True)
    t.start()
    with urllib.request.urlopen(f"http://127.0.0.1:{page.port}/status", timeout=5) as r:
        body = json.loads(r.read().decode())
    t.join(5)
    return body


def test_served_status_has_every_live_snapshot_key():
    lp = L.RunLoop(mode="paper", bankroll=5000)
    lp.on_frame({"type": "clock", "ts": T0})
    report = json.loads(json.dumps(lp.live_snapshot(), default=str))
    page = StatusPage(lambda: report, port=0)
    try:
        served = _get(page)
    finally:
        page.close()
    for key in NEW_KEYS:
        assert key in served, key
    assert served["fv_calibration"]["verdict"] == "insufficient_data"
    assert served["settled_positions_n"] == 0
    missing = sorted(set(report) - set(served))
    assert missing == [], f"live status keys not served on /status: {missing}"


def test_readiness_fv_criterion_reads_the_engine_report_shape():
    import argparse
    import importlib.util
    from pathlib import Path
    from mm.unattended import fv_calib as C
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("_next_rr2", root / "tools" / "readiness_report.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    cal = C.FVCalibration()
    for i in range(3):
        m = f"KXHIGHNY-26OCT0{i + 1}-T75"
        cal.record(m, "KXHIGHNY", 1.0, 70.0, 0.9, 30.0, 50.0, float(i))
        cal.on_settle(m, "yes")
    rep = json.loads(json.dumps(cal.report()))
    a = argparse.Namespace(min_fv_markets=3)
    c = R.crit_fv({"fv_calibration": rep}, a)
    assert c["value"] == {"n": 3.0, "model_brier": rep["overall"]["paired_brier_model"],
                          "book_brier": rep["overall"]["paired_brier_book"]}
    assert c["status"] == R.PASS          # 0.09 < 0.25
    a.min_fv_markets = 200
    assert R.crit_fv({"fv_calibration": rep}, a)["status"] == R.INSUFF
