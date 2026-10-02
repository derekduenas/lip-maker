"""verify_ws_frames: update_subscription / unsubscribe acks (ok) advance the
sid's seq as in the engine (no false delta gap); engine-detected gaps are
recorded by the dispatcher as ws_raw ``seq_gap`` rows and counted; --min-age-s
skips the recording the just-restarted engine is writing, and deploy.sh
uses it."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from mm.unattended import bookrec
from mm.unattended import loop as L
from tests.test_review_next_ws_raw import T0, V, _delta, _dispatch_record, _snap

ROOT = Path(__file__).resolve().parent.parent


def _ack_stream():
    return [
        {"type": "subscribed", "id": 1, "ts": T0 - 1, "msg": {"channel": "orderbook_delta", "sid": 1}},
        _snap(1, 1, "KXA-1", T0),
        _delta(1, 2, "KXA-1", T0 + 0.5),
        {"type": "ok", "id": 3, "ts": T0 + 1, "sid": 1, "seq": 3, "msg": {"market_tickers": ["KXA-1"]}},
        _delta(1, 4, "KXA-1", T0 + 2),
    ]


def test_ok_ack_seq_is_not_a_delta_gap(tmp_path):
    f, seen = _dispatch_record(tmp_path, _ack_stream())   # the engine raises no gap here
    rep = V.analyze(bookrec.iter_frames([f]), skew={"limit_s": 5.0, "n": 3, "sustain_s": 3.0})
    assert rep["seq"]["delta_gaps"] == 0 and rep["seq"]["engine_gaps"]["n"] == 0


def test_engine_gap_is_recorded_and_counted(tmp_path):
    rec = bookrec.FrameRecorder(str(tmp_path), min_free_gb=0.0, flush_s=0.0, rotate_s=1e9)
    rec.start()
    seqr, rows = L.SidSequencer(), []

    def on_frame(row):
        rows.append(row)
        rec.record(row)

    for msg in (_snap(1, 1, "KXA-1", T0), _delta(1, 2, "KXA-1", T0 + 1)):
        L._dispatch_ws_message(dict(msg), on_frame, seqr, None)
    with pytest.raises(L.SequenceGap):
        L._dispatch_ws_message(dict(_delta(1, 5, "KXA-1", T0 + 2)), on_frame, seqr, None)
    rec.stop()
    gap = [r for r in rows if r.get("type") == L.WS_RAW_TYPE and r["channel"] == "seq_gap"]
    assert len(gap) == 1
    assert gap[0]["msg"]["sid"] == 1 and gap[0]["msg"]["seq"] == 5 and gap[0]["msg"]["last_seq"] == 2
    assert gap[0]["msg"]["market_ticker"] == "KXA-1"
    rep = V.analyze(bookrec.iter_frames(bookrec.list_files(tmp_path)),
                    skew={"limit_s": 5.0, "n": 3, "sustain_s": 3.0})
    assert rep["seq"]["engine_gaps"]["n"] == 1
    assert "engine-detected seq gaps: 1" in V.render(dict(rep, files=[]))


def test_min_age_skips_the_file_being_written(tmp_path):
    now = time.time()
    names = []
    for age in (7200, 3600, 60):
        name = "frames-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now - age)) + ".jsonl.gz"
        (tmp_path / name).write_bytes(b"")
        names.append(name)
    got = V.resolve_paths([], str(tmp_path), 1, min_age_s=600, now=now)
    assert [Path(p).name for p in got] == [names[1]]
    assert [Path(p).name for p in V.resolve_paths([], str(tmp_path), 1, now=now)] == [names[2]]
    # a name without a start time falls back to the file's mtime
    odd = tmp_path / "frames-odd.jsonl.gz"
    odd.write_bytes(b"")
    os.utime(odd, (now - 30, now - 30))
    assert "frames-odd.jsonl.gz" not in [Path(p).name for p in
                                         V.resolve_paths([], str(tmp_path), 0, min_age_s=600, now=now)]


def test_deploy_runs_the_frame_check_on_a_settled_recording():
    text = (ROOT / "deploy" / "apex" / "deploy.sh").read_text()
    line = [x for x in text.splitlines() if "verify_ws_frames.py" in x and "--newest" in x][0]
    assert re.search(r"--newest 1\b", line) and re.search(r"--min-age-s 600\b", line)
