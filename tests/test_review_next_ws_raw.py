"""Raw websocket evidence reaches the recorder (ws_seq, lifecycle, replies).

Raw Kalshi-shaped messages go through the real loop._dispatch_ws_message
into the real bookrec.FrameRecorder, and tools/verify_ws_frames.py reads the
file back: the seq / lifecycle / reply checks run on what the engine
records. RunLoop and the replay bench must ignore the raw rows.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from mm.unattended import bookrec
from mm.unattended import loop as L

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_next_vwf", ROOT / "tools" / "verify_ws_frames.py")
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)

T0 = 1_790_000_000.0


class _Sock:
    def __init__(self):
        self.responses = []

    def note_response(self, msg):
        self.responses.append(msg)


def _snap(sid, seq, ticker, ts):
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "ts": ts,
            "sending_ts_ms": int((ts - 0.05) * 1000),
            "msg": {"market_ticker": ticker, "yes_dollars_fp": [["0.4500", "100.00"]],
                    "no_dollars_fp": [["0.5000", "80.00"]]}}


def _delta(sid, seq, ticker, ts):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq, "ts": ts,
            "sending_ts_ms": int((ts - 0.05) * 1000),
            "msg": {"market_ticker": ticker, "side": "yes", "price_dollars": "0.4500",
                    "delta_fp": "-10.00", "ts_ms": int((ts - 0.06) * 1000)}}


def _stream():
    return [
        {"type": "subscribed", "id": 1, "ts": T0 - 1, "msg": {"channel": "orderbook_delta", "sid": 1}},
        _snap(1, 1, "KXA-1", T0),
        _snap(1, 2, "KXB-1", T0 + 0.1),
        _delta(1, 3, "KXA-1", T0 + 0.5),
        {"type": "subscribed", "id": 2, "ts": T0 + 20, "msg": {"channel": "orderbook_delta", "sid": 1}},
        {"type": "ok", "id": 3, "ts": T0 + 21, "sid": 1, "seq": 3, "msg": {"market_tickers": ["KXC-1"]}},
        _snap(1, 6, "KXC-1", T0 + 30.0),          # merged batch: snapshot skips seq (resync)
        _delta(1, 7, "KXC-1", T0 + 30.5),
        {"type": "market_lifecycle_v2", "ts": T0 + 40, "sid": 9,
         "msg": {"market_ticker": "KXA-1", "event_type": "determined", "result": "yes"}},
        {"type": "market_lifecycle_v2", "ts": T0 + 41, "sid": 9,
         "msg": {"market_ticker": "KXB-1", "event_type": "close_date_updated"}},
    ]


def _dispatch_record(directory: Path, raw: list) -> tuple[Path, list]:
    rec = bookrec.FrameRecorder(str(directory), min_free_gb=0.0, flush_s=0.0, rotate_s=1e9)
    rec.start()
    seqr, sock = L.SidSequencer(), _Sock()
    seen = []

    def on_frame(row):
        seen.append(json.loads(json.dumps(row)))
        rec.record(row)

    for msg in raw:
        msg = json.loads(json.dumps(msg))
        ets = L._exchange_ts(msg)
        if ets is not None:
            msg["exchange_ts"] = ets
        L._dispatch_ws_message(msg, on_frame, seqr, sock)
    rec.stop()
    files = bookrec.list_files(directory)
    assert files
    return files[-1], seen


def test_dispatcher_keeps_ws_seq_and_forwards_raw_rows(tmp_path):
    _f, seen = _dispatch_record(tmp_path, _stream())
    books = [r for r in seen if r.get("type") in ("orderbook_snapshot", "orderbook_delta")]
    assert [r["ws_seq"] for r in books] == [1, 2, 3, 6, 7]
    assert all(r["seq"] is None for r in books)          # per-market books still see seq=None
    raw = [r for r in seen if r.get("type") == L.WS_RAW_TYPE]
    assert [r["channel"] for r in raw] == ["subscribed", "subscribed", "ok",
                                           "market_lifecycle_v2", "market_lifecycle_v2"]
    assert raw[2]["msg"]["seq"] == 3 and raw[2]["ts"] == T0 + 21
    # the derived settlement frame is still sent for determined/settled yes/no
    assert [r["market"] for r in seen if r.get("kind") == "settlement"] == ["KXA-1"]


def test_verify_ws_frames_runs_seq_lifecycle_and_reply_checks_on_engine_recording(tmp_path, capsys):
    f, _seen = _dispatch_record(tmp_path, _stream())
    rep = V.analyze(bookrec.iter_frames([f]), skew={"limit_s": 5.0, "n": 3, "sustain_s": 3.0})
    assert rep["failures"] == []
    assert rep["seq"]["recorded"] and rep["seq"]["source"] == {"ws_seq": 5}
    assert rep["seq"]["snapshot_gaps"] == 1 and rep["seq"]["delta_gaps"] == 0
    assert rep["lifecycle"]["raw"]["n"] == 2
    assert rep["lifecycle"]["raw"]["event_types"] == {"determined": 1, "close_date_updated": 1}
    assert rep["lifecycle"]["raw"]["with_result"] == 1
    replies = rep["subscriptions"]["replies"]
    assert replies["subscribed"] == 2 and replies["ok"] == 1 and replies["ok_with_seq"] == 1
    assert rep["subscriptions"]["sids_in_multiple_replies"] == [
        {"epoch": 0, "sid": 1, "replies": ["subscribed", "subscribed", "ok"]}]
    assert rep["ws_raw_rows"] == {"subscribed": 2, "ok": 1, "market_lifecycle_v2": 2}
    notes = " ".join(rep["notes"])
    assert "not recorded" not in notes
    assert V.main([str(f), "--unit-config", str(tmp_path / "none.conf")]) == 0
    out = capsys.readouterr().out
    assert "seq ({'ws_seq': 5})" in out


def test_runloop_ignores_raw_rows():
    lp = L.RunLoop(mode="paper", bankroll=5000)
    lp.on_frame({"type": "clock", "ts": T0})
    before = (lp.now, len(lp.settled), dict(lp.pulls))
    lp.on_frame({"type": L.WS_RAW_TYPE, "ts": T0 + 999, "channel": "market_lifecycle_v2",
                 "msg": {"type": "market_lifecycle_v2", "msg": {"market_ticker": "KXA-1",
                                                                "event_type": "settled", "result": "yes"}}})
    lp.on_frame({"type": L.WS_RAW_TYPE, "ts": T0 + 999, "channel": "ok",
                 "msg": {"type": "ok", "sid": 1, "seq": 4}})
    assert (lp.now, len(lp.settled), dict(lp.pulls)) == before


def test_replay_bench_skips_raw_rows(tmp_path):
    from mm import replay_bench as RB
    f, _seen = _dispatch_record(tmp_path, _stream())
    out = RB.run_one([f], {})
    assert L.WS_RAW_TYPE not in out["kinds"]
    # the window ends at the derived settlement frame (T0 + 40), not at the
    # raw close_date_updated row (T0 + 41)
    assert out["first_ts"] == T0 and out["last_ts"] == T0 + 40


def test_old_recordings_still_report_not_recorded(tmp_path):
    """A recording without raw rows (engine before this change) keeps the
    explicit not-recorded notes instead of guessing."""
    rec = bookrec.FrameRecorder(str(tmp_path), min_free_gb=0.0, flush_s=0.0, rotate_s=1e9).start()
    rec.record(dict(_snap(1, 1, "KXA-1", T0), seq=None))
    rec.stop()
    rep = V.analyze(bookrec.iter_frames(bookrec.list_files(tmp_path)),
                    skew={"limit_s": 5.0, "n": 3, "sustain_s": 3.0})
    notes = " ".join(rep["notes"])
    assert "seq not recorded" in notes and "subscribed/ok replies not recorded" in notes
    assert rep["ws_raw_rows"] == {}
    _ = time
