"""Dead-man for a Kalshi quote loop.

A quiet market is not a healthy feed. The socket has to beat. Silence
longer than ``stale_ms`` is a stall, a dropped socket is a drop, and a
killed risk engine is a kill. The first trip calls ``cancel`` once.
"""
from __future__ import annotations

from typing import Callable, Optional


class FeedClock:
    def __init__(self) -> None:
        self.connected = True
        self.ws_beat: Optional[float] = None
        self.rest_beat: Optional[float] = None
        self.loop_beat: Optional[float] = None

    def beat_ws(self, now: float) -> None:
        self.ws_beat = float(now)
        self.connected = True

    def beat_rest(self, now: float) -> None:
        self.rest_beat = float(now)

    def beat_loop(self, now: float) -> None:
        self.loop_beat = float(now)

    def drop(self) -> None:
        self.connected = False


class DeadMan:
    def __init__(self, clock: FeedClock, cancel: Callable[[str], None], *,
                 stale_ms: float = 3000, risk=None) -> None:
        self.clock = clock
        self.cancel = cancel
        self.stale_ms = float(stale_ms)
        self.risk = risk
        self.fired = False
        self.reason = ""

    def _age_ms(self, beat: Optional[float], now: float) -> Optional[float]:
        if beat is None:
            return None
        return (float(now) - float(beat)) * 1000.0

    def check(self, now: float) -> str:
        if self.fired:
            return self.reason
        reason = ""
        if self.risk is not None and getattr(self.risk, "killed", False):
            reason = "risk_kill"
        elif not self.clock.connected:
            reason = "socket_dropped"
        else:
            ws_age = self._age_ms(self.clock.ws_beat, now)
            if ws_age is None or ws_age > self.stale_ms:
                reason = "market_data_stale"
            else:
                rest_age = self._age_ms(self.clock.rest_beat, now)
                if rest_age is not None and rest_age > self.stale_ms:
                    reason = "rest_stale"
        if not reason:
            return ""
        self.fired = True
        self.reason = reason
        self.cancel(reason)
        return reason
