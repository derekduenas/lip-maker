"""tools/verify_ws_frames.py against recordings written by the real recorder.

The engine pipeline is reproduced exactly: raw Kalshi-shaped websocket
messages get ``ts`` / ``exchange_ts`` the way _readonly_books_session sets
them, go through loop._dispatch_ws_message, and whatever reaches on_frame
is written by bookrec.FrameRecorder (the APEX recorder).
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import shutil
import time
from pathlib import Path

import pytest

from mm.unattended import bookrec
from mm.unattended import loop as L

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_ops_vwf", ROOT / "tools" / "verify_ws_frames.py")
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)

T0 = 1_790_000_000.0


class _Sock:
    def __init__(self):
        self.responses = []

    def note_response(self, msg):
        self.responses.append(msg)


def _drain_stop(rec, n_expected=None):
    # FrameRecorder.stop() drains the queue (bounded); waiting here as well
    # keeps these tests independent of that bound.
    deadline = time.time() + 5
    while time.time() < deadline and (rec.q.qsize() or (
            n_expected is not None and rec.stats["frames"] < n_expected)):
        time.sleep(0.005)
    rec.stop()


def _record(directory: Path, frames_or_raw, *, through_dispatch=True) -> Path:
    rec = bookrec.FrameRecorder(str(directory), min_free_gb=0.0, flush_s=0.0, rotate_s=1e9)
    rec.start()
    seqr, sock = L.SidSequencer(), _Sock()
    for msg in frames_or_raw:
        msg = json.loads(json.dumps(msg))
        if through_dispatch and "kind" not in msg:
            ets = L._exchange_ts(msg)
            if ets is not None:
                msg["exchange_ts"] = ets
            L._dispatch_ws_message(msg, rec.record, seqr, sock)
        else:
            rec.record(msg)
    _drain_stop(rec)
    files = bookrec.list_files(directory)
    assert files, "recorder wrote nothing"
    return files[-1]


def _snap(sid, seq, ticker, ts, *, send_lag=0.05, sending=True):
    m = {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "ts": ts,
         "msg": {"market_ticker": ticker, "yes_dollars_fp": [["0.4500", "100.00"]],
                 "no_dollars_fp": [["0.5000", "80.00"]]}}
    if sending:
        m["sending_ts_ms"] = int((ts - send_lag) * 1000)
    return m


def _delta(sid, seq, ticker, ts, *, send_lag=0.05, sending=True, ts_ms=True):
    m = {"type": "orderbook_delta", "sid": sid, "seq": seq, "ts": ts,
         "msg": {"market_ticker": ticker, "side": "yes", "price_dollars": "0.4500",
                 "delta_fp": "-10.00"}}
    if ts_ms:
        m["msg"]["ts_ms"] = int((ts - send_lag - 0.01) * 1000)
    if sending:
        m["sending_ts_ms"] = int((ts - send_lag) * 1000)
    return m


def _healthy_stream():
    return [
        {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}},
        _snap(1, 1, "KXA-1", T0),
        _snap(1, 2, "KXB-1", T0 + 0.1),
        _delta(1, 3, "KXA-1", T0 + 0.5),
        _delta(1, 4, "KXB-1", T0 + 1.0),
        # later subscribe batch merged into sid 1: seq skips at its snapshot
        {"type": "ok", "id": 2, "sid": 1, "seq": 4, "msg": {"sid": 1}},
        _snap(1, 7, "KXC-1", T0 + 30.0),
        _delta(1, 8, "KXC-1", T0 + 30.5),
        {"type": "market_lifecycle_v2", "ts": T0 + 40, "sid": 9,
         "msg": {"market_ticker": "KXA-1", "event_type": "determined", "result": "yes"}},
        {"type": "market_lifecycle_v2", "ts": T0 + 41, "sid": 9,
         "msg": {"market_ticker": "KXB-1", "event_type": "close_date_updated"}},
    ]


def _skew():
    return {"limit_s": 5.0, "n": 3, "sustain_s": 3.0}


def test_healthy_engine_recording(tmp_path, capsys):
    f = _record(tmp_path, _healthy_stream())
    rep = V.analyze(bookrec.iter_frames([f]), skew=_skew())
    assert rep["failures"] == []
    assert rep["type_counts"] == {"orderbook_snapshot": 3, "orderbook_delta": 3, "settlement": 1}
    assert rep["book"]["orderbook_snapshot"]["pct_sending_ts_ms"] == 100.0
    assert rep["book"]["orderbook_delta"]["pct_msg_ts_ms"] == 100.0
    eng = rep["skew"]["recv_minus_exchange_s"]["engine"]
    assert eng["n"] == 6 and 0.0 < eng["p50"] < 0.1
    assert rep["skew"]["over_limit"]["engine"]["n"] == 0
    # merge evidence from book frames: KXC-1 first snapshot 30 s into sid 1
    late = rep["subscriptions"]["late_tickers_on_sid"]
    assert [r["ticker"] for r in late] == ["KXC-1"]
    # what the current engine does NOT record is reported, not guessed
    assert rep["seq"]["recorded"] is False
    notes = " ".join(rep["notes"])
    assert "seq not recorded" in notes
    assert "raw market_lifecycle_v2 messages not recorded" in notes
    assert "subscribed/ok replies not recorded" in notes
    s = rep["lifecycle"]["settlement_frames"]
    assert s["n"] == 1 and s["samples"][0]["market"] == "KXA-1" and s["samples"][0]["result"] == "yes"
    # CLI
    assert V.main([str(f), "--unit-config", str(tmp_path / "none.conf")]) == 0
    out = capsys.readouterr().out
    assert "OK: every engine-required field" in out and "seq: not recorded" in out


def test_missing_exchange_time_fails(tmp_path, capsys):
    raw = [_snap(1, 1, "KXA-1", T0, sending=False),
           _delta(1, 2, "KXA-1", T0 + 1, sending=False, ts_ms=False)]
    f = _record(tmp_path, raw)
    rc = V.main([str(f), "--unit-config", str(tmp_path / "none.conf")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "exchange time (clock-skew guard)" in out and "MISSING" in out


def test_partial_sending_ts_is_reported_not_failed(tmp_path):
    raw = [_snap(1, 1, "KXA-1", T0), _delta(1, 2, "KXA-1", T0 + 1, sending=False)]
    f = _record(tmp_path, raw)
    rep = V.analyze(bookrec.iter_frames([f]), skew=_skew())
    assert rep["failures"] == []
    assert rep["book"]["orderbook_delta"]["pct_sending_ts_ms"] == 0.0
    assert rep["skew"]["recv_minus_exchange_s"]["msg.ts_ms"]["n"] == 1
    assert rep["skew"]["recv_minus_exchange_s"]["sending_ts_ms"]["n"] == 1
    assert rep["skew"]["recv_minus_exchange_s"]["engine"]["n"] == 2


def test_skew_over_limit_and_guard_replay(tmp_path):
    raw = [_snap(1, 1, "KXA-1", T0)]
    raw += [_delta(1, 2 + i, "KXA-1", T0 + 1 + i, send_lag=7.0) for i in range(3)]
    raw += [_delta(1, 5, "KXA-1", T0 + 5)]
    f = _record(tmp_path, raw)
    rep = V.analyze(bookrec.iter_frames([f]), skew=_skew())
    sk = rep["skew"]
    assert sk["over_limit"]["sending_ts_ms"]["n"] == 3
    assert sk["guard_would_pull"] == 1
    assert sk["recv_minus_exchange_s"]["engine"]["max"] == pytest.approx(7.0, abs=0.01)


def test_skew_default_read_from_loop(tmp_path, monkeypatch):
    for k in V.SKEW_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = V.engine_skew_config(unit_configs=())
    assert cfg["limit_s"] == pytest.approx(L.CLOCK_SKEW_LIMIT_S)
    assert cfg["sources"]["LIP_CLOCK_SKEW_LIMIT_S"].endswith("loop.py")
    conf = tmp_path / "policy.conf"
    conf.write_text("[Service]\nEnvironment=LIP_CLOCK_SKEW_LIMIT_S=7\nEnvironment=LIP_CLOCK_SKEW_N=4\n")
    envf = tmp_path / "lip-maker.env"
    envf.write_text("LIP_CLOCK_SKEW_N=6\nOTHER_SECRET=nope\n")
    cfg = V.engine_skew_config(unit_configs=(str(conf), str(envf)))
    assert cfg["limit_s"] == 7.0 and cfg["n"] == 6
    monkeypatch.setenv("LIP_CLOCK_SKEW_LIMIT_S", "9")
    assert V.engine_skew_config(unit_configs=(str(conf),))["limit_s"] == 9.0
    assert V.engine_skew_config(cli_limit=2.5, unit_configs=(str(conf),))["limit_s"] == 2.5


def test_raw_capture_with_replies_lifecycle_and_seq(tmp_path):
    """A capture that keeps raw replies, lifecycle and the original seq."""
    frames = [
        {"type": "subscribed", "id": 1, "ts": T0, "msg": {"channel": "orderbook_delta", "sid": 1}},
        {"type": "orderbook_snapshot", "sid": 1, "seq": None, "ws_seq": 1, "ts": T0 + 0.1,
         "sending_ts_ms": int(T0 * 1000), "msg": {"market_ticker": "KXA-1", "yes_dollars_fp": []}},
        {"type": "orderbook_delta", "sid": 1, "seq": None, "ws_seq": 2, "ts": T0 + 0.2,
         "sending_ts_ms": int(T0 * 1000) + 100,
         "msg": {"market_ticker": "KXA-1", "side": "no", "price_dollars": "0.5", "delta_fp": "1"}},
        {"type": "subscribed", "id": 2, "ts": T0 + 1, "msg": {"channel": "orderbook_delta", "sid": 1}},
        {"type": "ok", "id": 3, "ts": T0 + 2, "sid": 1, "seq": 2, "msg": {"market_tickers": ["KXA-1"]}},
        {"type": "orderbook_snapshot", "sid": 1, "seq": None, "ws_seq": 5, "ts": T0 + 3,
         "sending_ts_ms": int(T0 * 1000) + 2900, "msg": {"market_ticker": "KXB-1", "no_dollars_fp": []}},
        {"type": "orderbook_delta", "sid": 1, "seq": None, "ws_seq": 8, "ts": T0 + 4,
         "sending_ts_ms": int(T0 * 1000) + 3900,
         "msg": {"market_ticker": "KXB-1", "side": "no", "price_dollars": "0.5", "delta_fp": "1"}},
        {"type": "market_lifecycle_v2", "ts": T0 + 5,
         "msg": {"market_ticker": "KXA-1", "event_type": "settled"}},
    ]
    f = _record(tmp_path, frames, through_dispatch=False)
    rep = V.analyze(bookrec.iter_frames([f]), skew=_skew())
    assert rep["seq"]["recorded"] and rep["seq"]["source"] == {"ws_seq": 4}
    assert rep["seq"]["snapshot_gaps"] == 1 and rep["seq"]["delta_gaps"] == 1
    multi = rep["subscriptions"]["sids_in_multiple_replies"]
    assert multi == [{"epoch": 0, "sid": 1, "replies": ["subscribed", "subscribed", "ok"]}]
    assert rep["subscriptions"]["replies"]["ok_with_seq"] == 1
    assert rep["lifecycle"]["raw"]["event_types"] == {"settled": 1}
    assert any("msg.result absent" in x for x in rep["failures"])


def test_connections_split_sids_and_pmus_skipped(tmp_path):
    raw = [_snap(1, 1, "KXA-1", T0),
           {"kind": "disconnect", "ts": T0 + 1, "reason": "x"},
           _snap(1, 1, "KXB-1", T0 + 60),
           {"type": "orderbook_snapshot", "ts": T0 + 61,
            "msg": {"market_ticker": "PMUS:some-slug", "yes_dollars_fp": [], "no_dollars_fp": []}}]
    f = _record(tmp_path, raw)
    rep = V.analyze(bookrec.iter_frames([f]), skew=_skew())
    assert rep["connections"] == 2
    assert rep["subscriptions"]["late_tickers_on_sid"] == []   # new connection, new sid 1
    assert rep["pmus_book_frames_skipped"] == 1
    assert rep["failures"] == []


def test_newest_and_no_files(tmp_path, capsys):
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    for i, name in enumerate(("frames-20261001T000000Z.jsonl.gz", "frames-20261001T010000Z.jsonl.gz",
                              "frames-20261001T020000Z.jsonl.gz")):
        sub = tmp_path / f"w{i}"
        sub.mkdir()
        f = _record(sub, [_snap(1, 1, f"KX{i}-1", T0 + i * 3600)])
        shutil.move(str(f), rec_dir / name)
    # a truncated file being written is tolerated
    with gzip.open(rec_dir / "frames-20261001T030000Z.jsonl.gz", "wt") as fh:
        fh.write('{"type":"orderbook_snapshot","sid":1,"ts":1,"msg":{"market_t')
    files = V.resolve_paths([], str(rec_dir), 2)
    assert [Path(p).name for p in files] == ["frames-20261001T020000Z.jsonl.gz",
                                             "frames-20261001T030000Z.jsonl.gz"]
    rc = V.main(["--dir", str(rec_dir), "--newest", "3", "--json", "--unit-config", "/nonexistent"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["type_counts"] == {"orderbook_snapshot": 2} and len(rep["files"]) == 3
    assert V.main(["--dir", str(tmp_path / "empty")]) == 2


def test_header_rows_counted_separately(tmp_path):
    rec = bookrec.FrameRecorder(str(tmp_path), min_free_gb=0.0, flush_s=0.0, rotate_s=1e9)
    rec.header[("program", "KXA-1")] = {"kind": "program", "market": "KXA-1"}
    rec.start()
    rec.record(_snap(1, 1, "KXA-1", T0) | {"seq": None})
    _drain_stop(rec, 1)
    rep = V.analyze(bookrec.iter_frames(bookrec.list_files(tmp_path)), skew=_skew())
    assert rep["header_counts"] == {"program": 1}
    assert rep["type_counts"] == {"orderbook_snapshot": 1}
