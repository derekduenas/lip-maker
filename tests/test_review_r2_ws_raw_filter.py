"""Raw market_lifecycle_v2 rows (the channel carries every Kalshi market)
are recorded only for markets this engine has a program, a position, a
calibration watch or pending calibration samples for; replies and seq_gap
rows always. The row is filtered and serialised off loop.lock."""
from types import SimpleNamespace

from mm.unattended import loop as L
from mm.unattended import service as S
from tests.test_fv_quote import MKT, _FV, _on, _prog, _env  # noqa: F401
from tests.test_patch15 import T0


def _lc(market, ev="determined"):
    return L._ws_raw_row("market_lifecycle_v2", {"type": "market_lifecycle_v2", "ts": T0, "sid": 9,
                                                 "msg": {"market_ticker": market, "event_type": ev,
                                                         "result": "yes"}})


class _Rec:
    def __init__(self, lock):
        self.lock, self.rows = lock, []

    def record(self, row):
        assert not self.lock._is_owned()            # serialised off the frame lock
        self.rows.append(row)


def test_lifecycle_rows_only_for_relevant_markets(monkeypatch):
    _on(monkeypatch)
    lp = L.RunLoop(mode="paper", bankroll=5000)
    lp.fv = _FV(50.0)
    _prog(lp)                                       # program + calibration watch
    eng = SimpleNamespace(loop=lp, rec=_Rec(lp.lock))
    lp.fv_calib.record("KXHIGHCHI-26OCT01-B60.5", "KXHIGHCHI", T0, 40.0, 0.9, 5.0, None, fv_ts=1.0)
    lp.position["KXHELD-1"] = {"venue": "kalshi"}
    for m in (MKT, "KXHIGHCHI-26OCT01-B60.5", "KXHELD-1", "KXUNRELATED-1"):
        S._Engine.on_frame(eng, _lc(m))
    S._Engine.on_frame(eng, L._ws_raw_row("ok", {"type": "ok", "sid": 1, "seq": 3}))
    S._Engine.on_frame(eng, L._ws_raw_row("seq_gap", {"type": "seq_gap", "sid": 1, "seq": 9}))
    got = [(r["channel"], (r["msg"].get("msg") or {}).get("market_ticker")) for r in eng.rec.rows]
    assert got == [("market_lifecycle_v2", MKT), ("market_lifecycle_v2", "KXHIGHCHI-26OCT01-B60.5"),
                   ("market_lifecycle_v2", "KXHELD-1"), ("ok", None), ("seq_gap", None)]
    assert lp.ws_raw_wanted(_lc("KXUNRELATED-1")) is False


def test_non_raw_frames_still_go_through_the_loop_under_the_lock():
    lp = L.RunLoop(mode="paper", bankroll=5000)
    seen = []

    class Rec:
        def record(self, row):
            assert lp.lock._is_owned()
            seen.append(row)
    eng = SimpleNamespace(loop=lp, rec=Rec())
    S._Engine.on_frame(eng, {"type": "clock", "ts": T0})
    assert seen and lp.now == T0


def test_service_constant_matches_the_loop():
    assert S.WS_RAW_TYPE == L.WS_RAW_TYPE
