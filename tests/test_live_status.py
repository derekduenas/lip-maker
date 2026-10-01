"""Live status refresh: read-only snapshot, throttled, never finishes the loop."""
from decimal import Decimal

from mm.status_page import status_payload
from mm.unattended.loop import RunLoop
from mm.unattended.service import LiveStatusRefresher


def _loop():
    loop = RunLoop(mode="paper")
    loop.add_program({"market": "KXTEST-1", "series": "KXTEST", "period_reward_usd": 50,
                      "period_seconds": 86400, "start_ts": 0, "end_ts": 4e9, "target_size": 100})
    loop.add_program({"market": "KXTEST-2", "series": "KXTEST", "period_reward_usd": 20,
                      "period_seconds": 86400, "start_ts": 0, "end_ts": 4e9, "target_size": 100})
    loop.now = 1_000.0
    loop.resting["KXTEST-1"] = {"yes": 10.0, "no": 10.0, "yes_cents": 40, "no_cents": 55}
    loop.quotes.append({"market": "KXTEST-1", "size": 10.0, "yes_cents": 40, "no_cents": 55,
                        "paper": True, "ts": 1_000.0})
    loop.selection_count = 1
    return loop


def test_snapshot_is_read_only_and_counts():
    loop = _loop()
    before = (dict(loop.open_seconds), list(loop.cancels), dict(loop.resting))
    snap = loop.live_snapshot(estimates={"KXTEST-1": Decimal("0.5")}, session_start_ts=1_000.0 - 3600)
    assert (dict(loop.open_seconds), list(loop.cancels), dict(loop.resting)) == before
    assert snap["paper"] is True and snap["live_armed"] is False
    assert snap["stage"] == "running"
    assert snap["programs_loaded"] == 2 and snap["selected_n"] == 1 and snap["quotes_n"] == 1
    assert snap["markets"] == ["KXTEST-1"]
    assert snap["selected_top"][0]["est_usd_per_day"] == "12.0000"
    assert snap["estimated_usd"] == "0.5"


def test_refresher_throttles_and_never_finishes(monkeypatch):
    loop = _loop()
    monkeypatch.setattr(loop, "finish", lambda: (_ for _ in ()).throw(AssertionError("finish called")))
    t = [0.0]
    writes = []
    r = LiveStatusRefresher(loop, writes.append, data_source="production-books", ws_url="wss://x",
                            clock=lambda: t[0])
    assert r.maybe_refresh() is True
    t[0] = 5.0
    assert r.maybe_refresh() is False
    t[0] = 10.5
    assert r.maybe_refresh() is True
    assert len(writes) == 2 and writes[-1]["data_source"] == "production-books"
    payload = status_payload(writes[-1])
    assert payload["live_armed"] is False and payload["programs_loaded"] == 2
    assert payload["stage"] == "running"


def test_refresher_swallows_errors():
    loop = _loop()

    def boom(_):
        raise RuntimeError("disk full")

    r = LiveStatusRefresher(loop, boom, data_source="x", ws_url="y", clock=lambda: 0.0)
    assert r.maybe_refresh() is False


def test_status_payload_unchanged_without_live_fields():
    assert set(status_payload({})) == {"paper", "demo", "mode", "live_armed", "stage", "markets",
                                       "estimated_usd", "kill", "data_source"}
