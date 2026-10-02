"""Calibration runs without FV quoting (LIP_FV_CALIB_ENABLE, default on with
LIP_FV_ENABLE), the screen feeds model families early only in paper, and a
model (weather) fair value never reaches the defensive guard or markouts
outside paper, nor the guard when FV quoting is off for its family."""
import time

from mm.unattended import fairvalue as F
from mm.unattended import fv_weather as W
from mm.unattended import loop as L
from mm.unattended import screen as SC
from tests.test_fv_quote import MKT, _FV, _Http, _ens_payload, _meta_row, _on, _prog, _env  # noqa: F401
from tests.test_patch15 import T0, _book


def test_calibration_flag_defaults_on_with_the_cache(monkeypatch):
    assert F.fv_calib_enabled() and not F.fv_quote_enabled()
    assert F.fv_model_active("KXHIGHNY") and not F.fv_quote_active("KXHIGHNY")
    monkeypatch.setenv("LIP_FV_CALIB_ENABLE", "0")
    assert not F.fv_model_active("KXHIGHNY")
    monkeypatch.setenv("LIP_FV_CALIB_ENABLE", "1")
    monkeypatch.setenv("LIP_FV_ENABLE", "0")
    assert not F.fv_calib_enabled()


def test_loop_records_calibration_with_quoting_off(monkeypatch):
    loop = L.RunLoop(mode="paper", bankroll=5000)
    loop.fv = _FV(62.0)
    _prog(loop)
    _book(loop, MKT, [(30, 2000)], [(60, 2000)], T0)
    assert MKT in loop.fv_calib_watch and MKT in loop.fv_cache_targets()
    assert loop._fv_quote_row(MKT) is None              # quoting stays off
    loop._fv_calib_at = 0.0
    loop._fv_calib_tick(T0 + 100)
    assert MKT in loop.fv_calib.pending
    # the model row does not drive the defensive guard either (fv 62 vs mid 35)
    assert loop._fv_drop(MKT, T0 + 100) == ()
    monkeypatch.setenv("LIP_FV_CALIB_ENABLE", "0")
    off = L.RunLoop(mode="paper", bankroll=5000)
    off.fv = _FV(62.0)
    _prog(off)
    assert MKT not in off.fv_calib_watch


def test_cache_prices_model_family_for_calibration_only(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(F.time, "time", lambda: datetime(2026, 9, 30, 15, tzinfo=timezone.utc).timestamp())
    http = _Http(_ens_payload())
    cache = F.FairValueCache(session=http, sleep=lambda s: None)
    cache.note_market(MKT, {"strike_type": "between", "floor_strike": 72, "cap_strike": 73})
    try:
        cache.refresh([MKT])
    except RuntimeError:
        pass                                          # polymarket 503 in the stub
    assert cache.values[MKT]["source"] == W.SOURCE


def test_model_fv_reaches_the_guard_only_in_paper_with_quoting(monkeypatch):
    _on(monkeypatch)
    loop = L.RunLoop(mode="paper", bankroll=5000)
    loop.fv = _FV(62.0, conf=0.3)        # below LIP_FV_MIN_CONF: not a quoting row
    _prog(loop)
    _book(loop, MKT, [(30, 2000)], [(60, 2000)], T0)
    assert loop._fv_drop(MKT, T0 + 1) == ("no",)       # paper: defensive guard on the model row
    assert loop._fv_side_cents(MKT, "yes") == 62.0
    loop.mode = "demo"
    assert loop._fv_drop(MKT, T0 + 2) == ()
    assert loop._fv_side_cents(MKT, "yes") is None
    loop.fv.source = "polymarket"                       # external FV keeps its live guard
    assert loop._fv_drop(MKT, T0 + 3) == ("no",)


def test_screen_feeds_model_family_early_only_in_paper(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    now = T0
    close = datetime.fromtimestamp(now + 30 * 3600, timezone.utc).isoformat()
    cache = SC.MetaCache(str(tmp_path / "c.json"))
    cache.markets[MKT] = SC.market_meta(_meta_row(MKT, close), now)
    cache.series["KXHIGHNY"] = {"category": "Climate and Weather", "fee_type": "quadratic_with_maker_fees"}
    frame = {"market": MKT, "series": "KXHIGHNY", "period_reward_usd": 50, "period_seconds": 86400,
             "end_ts": now + 86400, "target_size": 1000}
    _on(monkeypatch)
    assert SC.screen([frame], cache, now=now)[0] == []                    # default: not paper
    assert [f["market"] for f in SC.screen([frame], cache, now=now, paper=True)[0]] == [MKT]
    cache.series.pop("KXHIGHNY")
    assert SC.needs_series([frame], cache, now=now) == []
    assert SC.needs_series([frame], cache, now=now, paper=True) == ["KXHIGHNY"]
