"""Review fixes, robustness: one RunLoop across reconnects, engine state file,
kill-file/heartbeat timer, paper enforced in code, bounded growth."""
import json

import pytest

from mm.unattended import loop as L
from mm.unattended import service as S
from tests.test_review_loop_pnl import (
    M, T0, apply_policy, newloop, program, snap, _filled_loop,
)



@pytest.fixture(autouse=True)
def _env(monkeypatch):
    apply_policy(monkeypatch)
    for name in ("LIP_MARKET_INV_CAP_USD", "LIP_EVENT_INV_CAP_USD", "LIP_SINGLE_FILL_CAP_USD",
                 "LIP_WD_MAX_CAPITAL_USD", "LIP_FORCE_PAPER", "LIP_DEMO"):
        monkeypatch.delenv(name, raising=False)
    import monitor.alerts
    monkeypatch.setattr(monitor.alerts, "alert", lambda *a, **k: None)


# ------------------------------------------------------------------ 16. state file
def test_state_file_round_trip_keeps_positions_and_inventory(tmp_path):
    path = tmp_path / "engine_state.json"
    lp = _filled_loop()
    lp.attach_state(str(path))
    lp._state_dirty = True
    assert lp.save_state()
    saved = json.loads(path.read_text())
    assert saved["position"][M]["yes"] == lp.position[M]["yes"]
    lp2 = newloop()
    lp2.on_frame(program(M))
    lp2.attach_state(str(path))
    assert lp2.kill is None and lp2.state_error is None
    assert lp2.position[M] == lp.position[M]
    assert lp2.fills_total == lp.fills_total == 1
    assert float(lp2.risk.market_usd[M]) == pytest.approx(float(lp.inv_committed[M]))
    assert lp2.bucket_report()["short"]["markout_usd"] == pytest.approx(lp.bucket_report()["short"]["markout_usd"])


def test_corrupt_state_file_refuses_to_quote_and_is_not_overwritten(tmp_path):
    path = tmp_path / "engine_state.json"
    path.write_text("{not json")
    lp = newloop()
    lp.on_frame(program(M))
    lp.attach_state(str(path))
    assert lp.kill is not None and lp.kill["reason"].startswith("state_file_unreadable")
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert not lp.resting  # fail closed: no quoting
    lp._state_dirty = True
    assert lp.save_state(force=True) is False
    assert path.read_text() == "{not json"


def test_internal_kill_persists_and_operator_can_reset(tmp_path, capsys):
    path = tmp_path / "engine_state.json"
    lp = _filled_loop()
    lp.attach_state(str(path))
    lp._latch_kill("fills_per_minute: test")
    assert lp.save_state()
    lp2 = newloop()
    lp2.attach_state(str(path))
    assert lp2.kill is not None and lp2.kill["reason"].startswith("fills_per_minute")
    assert S.reset_state_kill(str(path)) == 0
    lp3 = newloop()
    lp3.attach_state(str(path))
    assert lp3.kill is None and lp3.position[M]["yes"] > 0


def test_service_keeps_one_runloop_across_sessions(tmp_path, monkeypatch):
    import mm.unattended.loop as loop_mod
    import mm.venues.readonly as R
    monkeypatch.setenv("LIP_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("LIP_KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("LIP_PAPER", "true")
    monkeypatch.setattr(R, "book_source", lambda force_demo=False: {
        "reader": True, "ws_url": "wss://api.elections.kalshi.com/trade-api/ws/v2",
        "flag": "test-books", "key_id": "k", "key_path": "/x"})
    seen = []

    class Stop(Exception):
        pass

    async def fake_drive(books, on_frame, **kw):
        seen.append(on_frame.__self__.loop)
        if len(seen) == 1:
            on_frame(program(M))
            return  # driver gave up / socket closed
        raise Stop()

    monkeypatch.setattr(loop_mod, "drive_readonly_books", fake_drive)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    with pytest.raises(Stop):
        S.main(["--run", "--heartbeat", str(tmp_path / "hb"), "--cancel-log", str(tmp_path / "cancel")])
    assert len(seen) == 2 and seen[0] is seen[1]
    assert M in seen[1].programs


# ------------------------------------------------------------------ 17. timer / heartbeat
def test_refused_mode_writes_no_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("LIP_PAPER", "false")
    monkeypatch.setenv("LIP_DEMO", "false")
    hb = tmp_path / "hb"
    with pytest.raises(S.UnattendedRefused):
        S.main(["--run", "--heartbeat", str(hb), "--cancel-log", str(tmp_path / "cancel")])
    assert not hb.exists()


def test_timer_honors_kill_file_and_beats_without_frames(tmp_path):
    lp = _filled_loop()
    lp.on_frame(snap(M, T0 + 3, [(38, 2000)], [(55, 2000)]))
    lp._select(T0 + 3)
    assert lp.resting
    kill = tmp_path / "KILL"
    hb = tmp_path / "hb"
    timer = S.EngineTimer(lp, heartbeat=str(hb), kill_path=str(kill))
    timer.tick()
    assert hb.exists() and lp.kill is None
    kill.write_text(json.dumps({"reason": "watchdog:test"}))
    timer.tick()  # no market-data frame in between
    assert lp.kill is not None and lp.kill["reason"] == "external_kill:watchdog:test"
    assert not lp.resting


# ------------------------------------------------------------------ 18. paper in code
def test_force_paper_refuses_everything_but_paper():
    env = {"LIP_FORCE_PAPER": "1", "LIP_PAPER": "false", "LIP_DEMO": "true"}
    with pytest.raises(S.UnattendedRefused):
        L.resolve_mode(env)
    with pytest.raises(S.UnattendedRefused):
        L.resolve_mode({"LIP_FORCE_PAPER": "1", "LIP_PAPER": "false"})
    assert L.resolve_mode({"LIP_FORCE_PAPER": "1"}) == "paper"
    assert L.resolve_mode({"LIP_PAPER": "false", "LIP_DEMO": "true"}) == "demo"


# ------------------------------------------------------------------ 19. bounded growth
def test_ended_programs_are_pruned_from_feed_and_loop():
    lp = newloop()
    lp.on_frame(program(M, end_ts=T0 - 7200))
    lp.on_frame(snap(M, T0, [(40, 2000)], [(55, 2000)]))
    ctx = {"fed": {M: 1}, "sigs": {M: ()}}
    gone = L._prune_fed([], dict(ctx, ends={M: T0 - 7200}), lp.on_frame, now=T0)
    assert gone == [M]
    assert M not in lp.programs and M not in lp.accruals and M not in lp.open_seconds
    # still a candidate (e.g. a roll-over is coming): kept
    ctx2 = {"fed": {M: 1}, "sigs": {M: ()}}
    assert L._prune_fed([program(M, end_ts=T0 - 7200)], ctx2, lambda f: None, now=T0) == []


def test_loop_prunes_long_ended_programs_but_keeps_inventory():
    lp = _filled_loop()
    lp._fv_wanted.update({M, "OLD-1"})
    lp.programs[M].end_ts = T0 - 7 * 3600
    lp.prune_ended(T0)
    assert M not in lp.programs and M not in lp.accruals
    assert M in lp.position and M in lp.last_mid
    assert lp._fv_wanted == set()


def test_fill_history_is_bounded_but_counts_stay_exact(monkeypatch):
    monkeypatch.setattr(L, "LIST_CAP", 10)
    lp = _filled_loop()
    for i in range(25):
        lp._record_fill({"market_ticker": M, "side": "no", "price_cents": 50, "count": 1.0,
                         "trade_id": f"x{i}"}, T0 + 100 * i)
    assert len(lp.fills) <= 10
    assert lp.fills_total == 26
    assert lp.live_snapshot()["fills_n"] == 26
    assert lp.venue_report()["kalshi"]["fills_n"] == 26
