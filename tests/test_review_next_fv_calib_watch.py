"""Calibration keeps sampling a fed KXHIGH market's model fair value until
its close, also when it is not quoted, after its program left the loop, and
across a restart; quoting is unchanged."""
from __future__ import annotations

import json
import time

from mm.unattended import fv_weather as W
from mm.unattended import loop as L
from tests.test_fv_quote import MKT, _on, _prog, _env  # noqa: F401  (autouse env fixture)
from tests.test_patch15 import T0, _book


class _LeadFV:
    """Model rows whose settlement window ends ``lead_h`` hours from now."""

    def __init__(self, fv=62.0, lead_h=30.0):
        self.fv, self.lead_h, self.n = fv, lead_h, 0
        self.hints = {}

    def get(self, market, now=None):
        self.n += 1
        end = time.time() + self.lead_h * 3600.0
        return {"fv_cents": self.fv, "conf": 0.9, "source": W.SOURCE, "pm_question": "model",
                "thr": 20.0, "ts": float(self.n), "window": [end - 86400.0, end]}

    def note_market(self, market, meta):
        self.hints[market] = dict(meta)

    def summary(self):
        return {"enabled": True}


def _loop(monkeypatch, close_h=30.0):
    _on(monkeypatch)
    loop = L.RunLoop(mode="paper", bankroll=5000)
    loop.fv = _LeadFV()
    _prog(loop, close_h=close_h)
    _book(loop, MKT, [(30, 2000)], [(60, 2000)], T0)
    loop.now = T0
    return loop


def test_sampling_continues_after_the_program_left_until_close(monkeypatch):
    loop = _loop(monkeypatch, close_h=30.0)
    assert MKT in loop.fv_calib_watch and MKT in loop.fv_cache_targets()
    loop._fv_calib_tick(T0 + 1)
    loop.end_program(MKT)                     # program gone (pruned / ended) before close
    assert MKT not in loop._fv_wanted and MKT not in loop.programs
    assert MKT in loop.fv_cache_targets()     # the cache keeps pricing it for calibration
    loop.fv.lead_h = 5.0
    loop._fv_calib_tick(T0 + 100)
    pend = loop.fv_calib.pending[MKT]
    assert pend["station"] == "KXHIGHNY"
    assert pend["samples"]["0-12h"]["mid"] is None          # no book any more: model-only sample
    assert pend["samples"]["0-12h"]["fv"] == 62.0
    # quoting is untouched: nothing rests, no selector view without a program
    assert MKT not in loop.resting and loop._km(MKT) is None
    # past close: the watch ends, no more samples
    loop.fv.lead_h = 0.5
    loop._fv_calib_tick(T0 + 30 * 3600 + 1)
    assert MKT not in loop.fv_calib_watch and MKT not in loop.fv_cache_targets()
    assert "0-12h" in pend["samples"] and len(pend["samples"]) == 2
    loop.settle(MKT, "yes")
    assert loop.fv_calib.report()["by_lead"]["0-12h"]["n"] == 1


def test_short_lead_sample_while_fed_but_not_quoted(monkeypatch):
    """Inside the 24 h FV close gate the market is not quoted; with its
    program still fed it is sampled with the book mid (a paired sample)."""
    loop = _loop(monkeypatch, close_h=8.0)
    loop.fv.lead_h = 8.0
    loop._select(T0 + 1)
    assert MKT not in loop.resting
    loop._fv_calib_tick(T0 + 100)
    s = loop.fv_calib.pending[MKT]["samples"]["0-12h"]
    assert s["mid"] == 35.0


def test_watch_persists_across_restart(monkeypatch, tmp_path):
    loop = _loop(monkeypatch, close_h=30.0)
    loop._fv_calib_tick(T0 + 1)
    path = tmp_path / "state.json"
    loop.attach_state(str(path))
    assert loop.save_state(force=True)
    saved = json.loads(path.read_text())
    assert saved["fv_calib_watch"][MKT]["series"] == "KXHIGHNY"
    fresh = L.RunLoop(mode="paper", bankroll=5000)
    fresh.attach_state(str(path))
    assert fresh.kill is None
    assert MKT in fresh.fv_calib_watch and MKT not in fresh.programs   # restarted, not re-fed
    fresh.fv = _LeadFV(lead_h=6.0)
    fresh._fv_calib_tick(T0 + 200)
    assert "0-12h" in fresh.fv_calib.pending[MKT]["samples"]
    assert fresh.live_snapshot()["fv_calibration"]["watching_n"] == 1
    # a malformed section is refused like any other invalid state
    saved["fv_calib_watch"] = {MKT: {"series": "KXHIGHNY", "close_ts": "soon"}}
    path.write_text(json.dumps(saved))
    bad = L.RunLoop(mode="paper", bankroll=5000)
    bad.attach_state(str(path))
    assert bad.kill is not None and "state_file_unreadable" in bad.kill["reason"]


def test_only_model_families_are_watched(monkeypatch):
    loop = _loop(monkeypatch)
    _prog(loop, market="KXHIGHLAX-26OCT01-B72.5")             # unsupported station
    assert "KXHIGHLAX-26OCT01-B72.5" not in loop.fv_calib_watch
    monkeypatch.setenv("LIP_FV_QUOTE_FAMILIES", "KXRAIN")      # family switched off: no samples
    loop.end_program(MKT)
    loop.fv_calib = type(loop.fv_calib)()                     # forget the sample of the setup frame
    loop._fv_calib_tick(T0 + 100)
    assert MKT not in loop.fv_calib.pending


def test_service_fv_cache_targets_use_the_loop_view():
    import inspect
    from mm.unattended import service as S
    src = inspect.getsource(S._Engine.__init__)
    assert "fv_cache_targets" in src
