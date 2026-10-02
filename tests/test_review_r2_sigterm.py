"""SIGTERM stops the --run engine cleanly: the asyncio driver's main task is
cancelled at its next await point (never inside a frame), the existing
finally path runs (engine stop: final state save, recorder drain, samples
flush; summary history flush) and main returns 0. Run in subprocesses so a
real SIGTERM never reaches the test runner."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(tmp_path, body: str, timeout=60) -> dict:
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(body))
    env = dict(os.environ, PYTHONPATH=str(ROOT), LIP_PAPER="true", LIP_FORCE_PAPER="1")
    out = subprocess.run([sys.executable, str(script)], cwd=str(ROOT), env=env, capture_output=True,
                         text=True, timeout=timeout)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_cancel_lands_at_an_await_point_never_mid_frame(tmp_path):
    res = _run(tmp_path, """
        import asyncio, json, os, signal
        from mm.unattended import service as S
        st = {"frames": 0, "in_frame": False, "cancel_in_frame": None, "finally": False}
        async def drive():
            try:
                while True:
                    st["in_frame"] = True
                    if st["frames"] == 3:
                        os.kill(os.getpid(), signal.SIGTERM)    # arrives mid-frame
                    sum(range(300000))                          # the frame keeps running
                    st["frames"] += 1
                    st["in_frame"] = False
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                st["cancel_in_frame"] = st["in_frame"]
                raise
            finally:
                st["finally"] = True
        S._TERM.clear()
        stopped = asyncio.run(S._until_sigterm(drive()))
        print(json.dumps(dict(st, stopped=stopped)))
    """)
    assert res["stopped"] is True and res["finally"] is True
    # the frame the signal arrived in completed (the handler runs on a later
    # loop iteration); the cancellation landed at an await, between frames
    assert res["frames"] >= 4 and res["cancel_in_frame"] is False


_MAIN = """
    import asyncio, json, os, signal, sys, threading, time
    from mm.unattended import service as S, loop as L
    import mm.venues.readonly as RO
    calls = []
    MODE = sys.argv[1] if len(sys.argv) > 1 else "{mode}"
    class FakeEngine:
        def __init__(self, *a, **k): calls.append("init")
        def on_frame(self, msg): pass
        def settle_candidates(self): return []
        def reconnect_if_down(self): pass
        def mark_down(self, reason): calls.append("mark_down")
        def write_final(self): calls.append("write_final")
        def stop(self): calls.append("stop")
    async def fake_drive(books, on_frame, **kw):
        calls.append(["drive", kw.get("paper")])
        if "{mode}" == "in_session":
            os.kill(os.getpid(), signal.SIGTERM)
            while True:
                await asyncio.sleep(0.01)
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return None                      # clean close: main sleeps --interval
    S._Engine = FakeEngine
    L.drive_readonly_books = fake_drive
    RO.book_source = lambda force_demo=False: {{"reader": True, "ws_url": "wss://example.invalid/ws",
                                               "flag": "production-books"}}
    S.flush_summary_histories = lambda: calls.append("flush")
    t0 = time.time()
    rc = S.main(["--run", "--heartbeat", "{tmp}/hb", "--cancel-log", "{tmp}/cl", "--interval", "30"])
    print(json.dumps({{"rc": rc, "calls": calls, "elapsed": time.time() - t0}}))
"""


def test_main_returns_0_and_runs_the_finally_path_on_sigterm(tmp_path):
    res = _run(tmp_path, _MAIN.format(mode="in_session", tmp=tmp_path))
    assert res["rc"] == 0
    assert res["calls"] == ["init", ["drive", True], "mark_down", "stop", "flush"]


def test_sigterm_between_sessions_stops_without_waiting_the_interval(tmp_path):
    res = _run(tmp_path, _MAIN.format(mode="between", tmp=tmp_path))
    assert res["rc"] == 0 and res["elapsed"] < 10
    assert res["calls"] == ["init", ["drive", True], "mark_down", "stop", "flush"]
