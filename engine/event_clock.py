"""An injected event clock for replay, scoped to the strategy modules.

The problem with the obvious approach
-------------------------------------
Replay runs far faster than wall clock, but the runner's throttles
(scoring, reprice, persist) and its accounting are wall-clock based. Left
alone they suppress almost every evaluation. The first fix patched the
GLOBAL `time.time`, which is too blunt: the same clock is used by
networking, by asyncio bookkeeping and by anything else running in the
process, so a replay could perturb machinery that has nothing to do with
the strategy.

What this does instead
----------------------
`EventClock` is a tiny module-shaped object exposing the parts of `time`
that strategy code actually calls. `scoped()` swaps it into the namespaces
of named modules only — `run_paper`, `execution.quote_manager` — and puts
the originals back afterwards.

  * `time()` returns the EVENT time being replayed, so strategy decisions
    and accounting see the stream's own clock.
  * `monotonic()` and `perf_counter()` stay REAL, because operational
    timeouts and durations must not be distorted by replay.
  * `sleep()` stays real for the same reason.

Nothing outside the named modules is affected.
"""
from __future__ import annotations

import contextlib
import time as _real_time


class EventClock:
    """Module-shaped stand-in for `time` with a controllable wall clock."""

    def __init__(self, start: float | None = None):
        self._now = float(start if start is not None else _real_time.time())

    # the controllable part
    def time(self) -> float:
        return self._now

    def set(self, ts: float) -> None:
        self._now = float(ts)

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)

    # operational timing stays real on purpose
    def monotonic(self) -> float:
        return _real_time.monotonic()

    def perf_counter(self) -> float:
        return _real_time.perf_counter()

    def sleep(self, seconds):
        return _real_time.sleep(seconds)

    def __getattr__(self, name):
        # anything else (strftime, gmtime, ...) falls through untouched
        return getattr(_real_time, name)


DEFAULT_SCOPE = ("run_paper", "execution.quote_manager")


@contextlib.contextmanager
def scoped(clock: EventClock, modules=DEFAULT_SCOPE):
    """Swap `clock` in for `time` inside `modules`, then restore.

    Only the named modules are touched. Networking, asyncio and every other
    module keep the real clock.
    """
    import importlib
    saved = []
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if hasattr(mod, "time"):
            saved.append((mod, mod.time))
            mod.time = clock
    try:
        yield clock
    finally:
        for mod, original in saved:
            mod.time = original
