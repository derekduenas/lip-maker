"""FrameRecorder.stop() writes what is still queued (bounded) before it exits."""
from __future__ import annotations

import threading

from mm.unattended import bookrec


def _frames(directory):
    return list(bookrec.iter_frames(bookrec.list_files(directory)))


def test_stop_drains_queued_frames(tmp_path):
    rec = bookrec.FrameRecorder(str(tmp_path), min_free_gb=0.0, flush_s=0.0, rotate_s=1e9)
    gate = threading.Event()
    real_open = rec._open

    def slow_open(now):          # writer stalls on its first file: frames pile up in the queue
        gate.wait(5)
        real_open(now)

    rec._open = slow_open
    rec.start()
    for i in range(500):
        rec.record({"type": "orderbook_delta", "ts": 1.0 + i, "i": i})
    gate.set()
    rec.stop(timeout=5.0)
    got = _frames(tmp_path)
    assert [f["i"] for f in got] == list(range(500))
    assert rec.stats["frames"] == 500 and rec.stats["dropped"] == 0
    assert rec._thread is not None and not rec._thread.is_alive()


def test_stop_drain_is_bounded(tmp_path):
    rec = bookrec.FrameRecorder(str(tmp_path), min_free_gb=0.0, flush_s=0.0, rotate_s=1e9)
    block = threading.Event()

    def stuck_open(now):          # disk never comes back: stop must still return
        block.wait(30)

    rec._open = stuck_open
    rec.start()
    for i in range(10):
        rec.record({"type": "trade", "ts": float(i)})
    import time
    t0 = time.time()
    rec.stop(timeout=0.3)
    assert time.time() - t0 < 3.0
    block.set()


def test_stop_without_start_is_safe(tmp_path):
    rec = bookrec.FrameRecorder(str(tmp_path), min_free_gb=0.0)
    rec.record({"type": "trade", "ts": 1.0})
    rec.stop()
