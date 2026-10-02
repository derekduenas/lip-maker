"""Settlement backfill also asks about markets with pending fair-value
calibration samples past close, so samples whose settlement was missed while
disconnected get scored."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from mm.unattended import loop as L
from mm.unattended import service as S

T0 = 1_790_800_000.0
MKT = "KXHIGHNY-26OCT01-B72.5"


class _Reader:
    def __init__(self, results):
        self.results, self.asked = results, []

    def get(self, path):
        ticker = path.rsplit("/", 1)[-1]
        self.asked.append(ticker)
        res = self.results.get(ticker)
        return {"market": {"status": "determined" if res else "closed", "result": res or ""}}


def _loop_with_pending(close_ts, lead_h=5.0, ts=T0):
    lp = L.RunLoop(mode="paper", bankroll=5000)
    lp.fv_calib.record(MKT, "KXHIGHNY", ts, 62.0, 0.9, lead_h, 40.0, fv_ts=1.0)
    if close_ts is not None:
        lp.fv_calib_watch[MKT] = {"series": "KXHIGHNY", "close_ts": close_ts, "added_ts": ts}
    return lp


def test_pending_past_close_plus_delay_becomes_a_settle_candidate(monkeypatch):
    monkeypatch.delenv("LIP_FV_CALIB_BACKFILL_AFTER_S", raising=False)
    close = T0 + 5 * 3600
    lp = _loop_with_pending(close)
    lp._fv_settle_refresh(close + 3600)                 # 1 h after close: the websocket usually settles it
    assert lp.fv_settle_view == ()
    lp._fv_settle_refresh(close + 12 * 3600 + 1)
    assert lp.fv_settle_view == (MKT,)
    eng = SimpleNamespace(loop=lp)
    assert S._Engine.settle_candidates(eng) == [MKT]


def test_close_from_samples_when_not_watched(monkeypatch):
    monkeypatch.setenv("LIP_FV_CALIB_BACKFILL_AFTER_S", "0")
    lp = _loop_with_pending(None, lead_h=5.0, ts=T0)    # window end = T0 + 5 h
    lp._fv_settle_refresh(T0 + 4 * 3600)
    assert lp.fv_settle_view == ()
    lp._fv_settle_refresh(T0 + 5 * 3600 + 60)
    assert lp.fv_settle_view == (MKT,)


def test_held_positions_come_first(monkeypatch):
    monkeypatch.setenv("LIP_FV_CALIB_BACKFILL_AFTER_S", "0")
    lp = _loop_with_pending(T0)
    lp._fv_settle_refresh(T0 + 10)
    lp.settle_view = (("KXHELD-1", "kalshi"), ("PMUS:x", "pmus"), (MKT, "kalshi"))
    assert S._Engine.settle_candidates(SimpleNamespace(loop=lp)) == ["KXHELD-1", MKT]


def test_backfill_scores_the_missed_calibration_sample(monkeypatch):
    monkeypatch.setenv("LIP_FV_CALIB_BACKFILL_AFTER_S", "0")
    lp = _loop_with_pending(T0 + 3600)
    lp.on_frame({"type": "clock", "ts": T0 + 3600 + 120})   # loop second: refreshes the views
    assert lp.fv_settle_view == (MKT,)
    reader = _Reader({MKT: "no"})
    ctx = {"settle_candidates": lambda: S._Engine.settle_candidates(SimpleNamespace(loop=lp))}
    n = asyncio.run(L._settlement_backfill(reader, ctx, lp.on_frame, now=time.time()))
    assert n == 1 and reader.asked == [MKT]
    assert MKT not in lp.fv_calib.pending
    rep = lp.fv_calib.report()
    assert rep["overall"]["n"] == 1 and rep["overall"]["paired_n"] == 1
    assert MKT not in lp.settled                         # nothing held: no P&L row
    lp.on_frame({"type": "clock", "ts": T0 + 3600 + 200})
    lp._fv_settle_refresh(T0 + 3600 + 400)
    assert lp.fv_settle_view == ()
