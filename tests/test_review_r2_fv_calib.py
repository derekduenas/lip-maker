"""Calibration review fixes: the first PAIRED sample per lead bucket is kept
apart from the first (model-only) sample, paired distinct markets are
counted, every city-day event is scored over its full bucket distribution
(RPS and log score, model vs normalised book mids), low-confidence samples
are kept out of the headline, and the verdict needs enough paired markets
AND events. Settled samples carry the member-max summary for fitting."""
import json
import math
import time

import pytest

from mm.unattended import fv_calib as C
from mm.unattended import fv_weather as W
from mm.unattended import loop as L
from tests.test_fv_quote import _on, _env  # noqa: F401  (autouse env fixture)
from tests.test_patch15 import T0, _book

EV = "KXHIGHNY-26OCT01"
MKTS = (f"{EV}-T71", f"{EV}-B72.5", f"{EV}-T74")
RANGES = ([None, 71], [72, 73], [74, None])
ENS = {"mean": 72.4, "sd": 1.6, "n": 82, "floor_f": None, "slip": 0.1, "after_window": False}


def _event(c, fvs, mids, *, conf=0.9, lead_h=30.0, ts=T0, event=EV):
    entries = [{"market": m, "range": r, "fv": f, "conf": conf, "mid": q}
               for m, r, f, q in zip([f"{event}-{m.rsplit('-', 1)[1]}" for m in MKTS], RANGES, fvs, mids)]
    for e in entries:
        c.record(e["market"], "KXHIGHNY", ts, e["fv"], conf, lead_h, e["mid"], fv_ts=ts, rng=e["range"], ens=ENS)
    return c.record_event(event, "KXHIGHNY", ts, lead_h, entries)


def test_first_paired_sample_kept_apart_from_first_model_sample():
    c = C.FVCalibration()
    m = MKTS[1]
    assert c.record(m, "KXHIGHNY", T0, 60.0, 0.9, 30.0, None, fv_ts=1.0)       # no book yet
    assert c.record(m, "KXHIGHNY", T0 + 300, 62.0, 0.9, 29.9, 40.0, fv_ts=2.0)  # first paired
    assert not c.record(m, "KXHIGHNY", T0 + 600, 64.0, 0.9, 29.8, 45.0, fv_ts=3.0)
    pend = c.pending[m]
    assert pend["samples"]["24-48h"]["fv"] == 60.0 and pend["samples"]["24-48h"]["mid"] is None
    assert pend["paired"]["24-48h"]["fv"] == 62.0 and pend["paired"]["24-48h"]["mid"] == 40.0
    assert c.on_settle(m, "yes") == 1
    o = c.report()["overall"]
    assert o["n"] == 1 and o["paired_n"] == 1
    assert o["brier"] == pytest.approx(0.4 ** 2, abs=1e-6)                   # model-only sample: 60c
    assert o["paired_brier_model"] == pytest.approx(0.38 ** 2, abs=1e-6)      # paired sample: 62c
    assert o["paired_brier_book"] == pytest.approx(0.6 ** 2, abs=1e-6)
    assert c.report()["paired_markets"] == 1


def test_paired_markets_count_distinct_markets():
    c = C.FVCalibration()
    m = MKTS[1]
    c.record(m, "KXHIGHNY", T0, 60.0, 0.9, 30.0, 40.0, fv_ts=1.0)
    c.record(m, "KXHIGHNY", T0 + 20 * 3600, 70.0, 0.9, 10.0, 50.0, fv_ts=2.0)
    c.on_settle(m, "yes")
    rep = c.report()
    assert rep["overall"]["paired_n"] == 2 and rep["paired_markets"] == 1


def test_event_scored_over_the_full_distribution():
    c = C.FVCalibration()
    assert _event(c, (20.0, 50.0, 30.0), (30.0, 40.0, 30.0))
    for m, res in zip(MKTS, ("no", "yes", "no")):
        c.on_settle(m, res)
    ev = c.report()["events"]
    o = ev["overall"]
    assert o["n"] == 1 and o["paired_n"] == 1 and ev["paired_events"] == 1
    assert o["rps"] == pytest.approx(((0.2 - 0) ** 2 + (0.7 - 1) ** 2) / 2, abs=1e-5)
    assert o["paired_rps_book"] == pytest.approx(((0.3 - 0) ** 2 + (0.7 - 1) ** 2) / 2, abs=1e-5)
    assert o["paired_log_model"] == pytest.approx(-math.log(0.5), abs=1e-5)
    assert o["paired_log_book"] == pytest.approx(-math.log(0.4), abs=1e-5)
    assert o["rps_skill_vs_book"] > 0
    assert not c.events          # scored once, then forgotten


def test_event_needs_tiling_buckets_and_all_mids_for_pairing():
    c = C.FVCalibration()
    entries = [{"market": MKTS[0], "range": [None, 71], "fv": 20.0, "conf": 0.9, "mid": 30.0},
               {"market": MKTS[2], "range": [74, None], "fv": 30.0, "conf": 0.9, "mid": 30.0}]
    assert not c.record_event(EV, "KXHIGHNY", T0, 30.0, entries)            # 72-73 missing
    assert _event(c, (20.0, 50.0, 30.0), (30.0, None, 30.0))               # one mid missing
    e = c.events[EV]
    assert "24-48h" in e["samples"] and not e["paired"]
    # a later complete book pairs the event (first paired sample kept)
    assert _event(c, (25.0, 45.0, 30.0), (30.0, 40.0, 30.0), ts=T0 + 600)
    assert e["paired"]["24-48h"]["book"] == pytest.approx([0.3, 0.4, 0.3])
    assert e["samples"]["24-48h"]["model"] == pytest.approx([0.2, 0.5, 0.3])


def test_low_confidence_kept_out_of_the_headline(monkeypatch):
    monkeypatch.setenv("LIP_FV_MIN_CONF", "0.6")
    c = C.FVCalibration()
    _event(c, (20.0, 50.0, 30.0), (30.0, 40.0, 30.0), conf=0.3)
    for m, res in zip(MKTS, ("no", "yes", "no")):
        c.on_settle(m, res)
    rep = c.report()
    assert rep["overall"]["n"] == 0 and rep["events"]["overall"]["n"] == 0
    assert rep["low_conf"]["overall"]["n"] == 3 and rep["low_conf"]["events"]["overall"]["n"] == 1
    assert rep["paired_markets"] == 0 and rep["events"]["paired_events"] == 0


def test_verdict_needs_paired_markets_and_events(monkeypatch):
    monkeypatch.setenv("LIP_FV_CALIB_MIN_MARKETS", "3")
    monkeypatch.setenv("LIP_FV_CALIB_MIN_EVENTS", "2")
    c = C.FVCalibration()
    # many correlated per-market samples of ONE event: not enough events
    _event(c, (5.0, 90.0, 5.0), (30.0, 40.0, 30.0))
    for m, res in zip(MKTS, ("no", "yes", "no")):
        c.on_settle(m, res)
    rep = c.report()
    assert rep["paired_markets"] == 3 and rep["events"]["paired_events"] == 1
    assert rep["verdict"] == "insufficient_data" and not c.passed()
    _event(c, (5.0, 90.0, 5.0), (30.0, 40.0, 30.0), event="KXHIGHNY-26OCT02")
    for m, res in zip(MKTS, ("no", "yes", "no")):
        c.on_settle(m.replace("26OCT01", "26OCT02"), res)
    assert c.report()["verdict"] == "model_better_than_book" and c.passed()
    bad = C.FVCalibration()
    for ev in ("KXHIGHNY-26OCT01", "KXHIGHNY-26OCT02"):
        _event(bad, (45.0, 10.0, 45.0), (30.0, 40.0, 30.0), event=ev)
        for m, res in zip(MKTS, ("no", "yes", "no")):
            bad.on_settle(m.replace("26OCT01", ev.rsplit("-", 1)[1]), res)
    assert bad.report()["verdict"] == "book_better_or_equal" and not bad.passed()


def test_settled_rows_carry_the_member_summary_for_fitting():
    c = C.FVCalibration()
    _event(c, (20.0, 50.0, 30.0), (30.0, 40.0, 30.0))
    c.on_settle(MKTS[1], "yes")
    rows = c.take_outbox()
    assert len(rows) == 1 and c.take_outbox() == []
    r = rows[0]
    assert r["market"] == MKTS[1] and r["event"] == EV and r["station"] == "KXHIGHNY" and r["y"] == 1
    assert r["range"] == [72, 73] and r["ens"] == ENS and r["fv"] == 50.0 and r["mid"] == 40.0
    assert r["lead_bucket"] == "24-48h"


def test_state_roundtrip_and_legacy_aggregates():
    c = C.FVCalibration()
    _event(c, (20.0, 50.0, 30.0), (30.0, 40.0, 30.0))
    c.on_settle(MKTS[0], "no")
    blob = json.loads(json.dumps(c.state()))
    d = C.FVCalibration()
    d.load_state(blob)
    assert d.report()["overall"] == c.report()["overall"]
    assert EV in d.events and MKTS[1] in d.pending and d.pending[MKTS[1]]["samples"]["24-48h"]["ens"] == ENS
    for m, res in zip(MKTS[1:], ("yes", "no")):
        d.on_settle(m, res)
    assert d.report()["events"]["overall"]["n"] == 1
    # a pre-fix state (no version): its aggregates mixed confidence and the
    # pairing bug, so they are reported as legacy and never reach the verdict
    old = {"agg": {"KXHIGHNY|24-48h": {"n": 5, "brier": 0.5, "paired_n": 5, "paired_brier": 0.5,
                                         "book_brier": 1.0}},
           "pending": {MKTS[1]: {"station": "KXHIGHNY", "last_ts": T0, "samples": {
               "24-48h": {"ts": T0, "fv": 60.0, "conf": 0.9, "lead_h": 30.0, "mid": 40.0}}}},
           "scored_markets": 5, "dropped": 0}
    e = C.FVCalibration()
    e.load_state(old)
    rep = e.report()
    assert rep["overall"]["n"] == 0 and rep["legacy"]["overall"]["n"] == 5
    assert rep["legacy"]["scored_markets"] == 5
    assert e.pending[MKTS[1]]["paired"]["24-48h"]["mid"] == 40.0
    with pytest.raises(ValueError):
        C.FVCalibration().load_state({"version": 2, "agg": {"nobar": {}}})


# ---------------------------------------------------------------- loop wiring
class _EventFV:
    """Model rows for the three buckets of one city-day."""

    def __init__(self, fvs=(20.0, 50.0, 30.0), lead_h=30.0):
        self.fvs, self.lead_h, self.n = dict(zip(MKTS, fvs)), lead_h, 0
        self.hints = {}

    def get(self, market, now=None):
        if market not in self.fvs:
            return None
        end = time.time() + self.lead_h * 3600.0
        return {"fv_cents": self.fvs[market], "conf": 0.9, "source": W.SOURCE, "pm_question": "model",
                "thr": 20.0, "ts": 7.0, "window": [end - 86400.0, end],
                "range": RANGES[MKTS.index(market)], "ens": ENS}

    def note_market(self, market, meta):
        self.hints[market] = dict(meta)

    def summary(self):
        return {"enabled": True}


def test_loop_records_event_with_book_mids_and_scores_it(monkeypatch):
    _on(monkeypatch)
    loop = L.RunLoop(mode="paper", bankroll=5000)
    loop.fv = _EventFV()
    for m, mid in zip(MKTS, (30, 40, 30)):
        loop.add_program({"market": m, "series": "KXHIGHNY", "period_reward_usd": 100.0,
                          "period_seconds": 86400, "start_ts": T0 - 3600, "end_ts": T0 + 86400,
                          "close_ts": T0 + 30 * 3600, "target_size": 1000, "days_from_close": True,
                          "rank_score": 0.1, "exchange_index": 0})
        _book(loop, m, [(mid - 1, 2000)], [(100 - mid - 1, 2000)], T0)
    loop.fv_calib = C.FVCalibration()
    loop._fv_calib_at = 0.0
    loop._fv_calib_tick(T0 + 100)
    e = loop.fv_calib.events[EV]
    assert e["paired"]["24-48h"]["book"] == pytest.approx([0.3, 0.4, 0.3])
    assert loop.fv_calib.pending[MKTS[1]]["samples"]["24-48h"]["ens"] == ENS
    for m, res in zip(MKTS, ("no", "yes", "no")):
        loop.settle(m, res)
    ev = loop.live_snapshot()["fv_calibration"]["events"]
    assert ev["overall"]["paired_n"] == 1 and ev["paired_events"] == 1


def test_engine_timer_appends_settled_samples_off_the_lock(tmp_path, monkeypatch):
    from mm.unattended import service as S
    path = tmp_path / "samples.jsonl"
    monkeypatch.setenv("LIP_FV_CALIB_SAMPLES_FILE", str(path))
    lp = L.RunLoop(mode="paper", bankroll=5000)
    _event(lp.fv_calib, (20.0, 50.0, 30.0), (30.0, 40.0, 30.0))
    lp.fv_calib.on_settle(MKTS[1], "yes")
    timer = S.EngineTimer(lp, heartbeat=str(tmp_path / "hb"), kill_path=str(tmp_path / "KILL"))
    timer.tick()
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["market"] == MKTS[1] and rows[0]["ens"] == ENS
    timer.tick()
    assert len(path.read_text().splitlines()) == 1          # written once
    # an unwritable target keeps the rows queued for the next tick
    lp.fv_calib.on_settle(MKTS[0], "no")
    monkeypatch.setenv("LIP_FV_CALIB_SAMPLES_FILE", str(tmp_path / "f" / "x.jsonl"))
    (tmp_path / "f").write_text("not a dir")
    timer.tick()
    assert len(lp.fv_calib.outbox) == 1
    monkeypatch.setenv("LIP_FV_CALIB_SAMPLES_FILE", str(path))
    timer.tick()
    assert len(path.read_text().splitlines()) == 2 and not lp.fv_calib.outbox
