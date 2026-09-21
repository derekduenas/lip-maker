"""Replay uses an injected clock, scoped to the strategy modules.

An earlier version patched the GLOBAL time.time so the runner's wall-clock
throttles would behave during fast replay. That also reached networking,
asyncio bookkeeping and anything else in the process. This scopes the swap
to the modules whose decisions and accounting we actually want on stream
time, and keeps operational timing real.
"""
from __future__ import annotations

import sys
import time as real_time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.event_clock import EventClock, scoped


def test_event_time_is_controllable():
    c = EventClock(1000.0)
    assert c.time() == 1000.0
    c.set(2000.0); assert c.time() == 2000.0
    c.advance(5.0); assert c.time() == 2005.0


def test_operational_timing_stays_real():
    """Timeouts and durations must not be distorted by replay speed."""
    c = EventClock(1000.0)
    assert abs(c.monotonic() - real_time.monotonic()) < 1.0
    assert abs(c.perf_counter() - real_time.perf_counter()) < 1.0


def test_unknown_attributes_fall_through_to_real_time():
    c = EventClock(0.0)
    assert c.gmtime(0).tm_year == 1970


def test_scope_swaps_only_the_named_modules():
    import run_paper
    c = EventClock(1234.0)
    with scoped(c, modules=("run_paper",)):
        assert run_paper.time.time() == 1234.0
        # the process clock is untouched
        assert abs(real_time.time() - 1234.0) > 1e6
    assert run_paper.time is real_time


def test_scope_restores_on_exception():
    import run_paper
    c = EventClock(1.0)
    try:
        with scoped(c, modules=("run_paper",)):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert run_paper.time is real_time


def test_a_missing_module_is_skipped_not_fatal():
    c = EventClock(1.0)
    with scoped(c, modules=("definitely_not_a_module_xyz",)):
        pass


def test_quote_manager_is_in_the_default_scope():
    from engine.event_clock import DEFAULT_SCOPE
    assert "run_paper" in DEFAULT_SCOPE
    assert "execution.quote_manager" in DEFAULT_SCOPE
