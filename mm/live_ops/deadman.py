"""Off-VM dead-man's switch (healthchecks.io style; optional, paper-safe).

The watchdog runs on the same VM: if the VM dies, nothing pages. This pings an EXTERNAL URL
(``LIP_DEADMAN_URL``) while the engine is healthy; the external service pages when pings stop.
The URL is a secret: read from the environment only, never logged, never in /status, and never
included in an error (only the exception TYPE is kept). Pings are throttled to ``interval_s``
(failed pings count against the interval too) and skipped while unhealthy, so a stalled engine,
a latched kill or a dead feed stops the pings, which is the point.
"""
from __future__ import annotations

import os
import time
import urllib.request
from typing import Callable, Optional


def _default_fetch(url: str, timeout: float) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as resp:      # GET; the body is ignored
        resp.read(64)


class DeadMansSwitch:
    def __init__(self, url: Optional[str] = None, *, interval_s: float = 60.0, timeout_s: float = 5.0,
                 fetch: Callable[[str, float], object] = _default_fetch) -> None:
        self._url = url or None
        self.interval_s = float(interval_s)
        self.timeout_s = float(timeout_s)
        self._fetch = fetch
        self._last_attempt: Optional[float] = None
        self.last_ok_ts: Optional[float] = None
        self.failures = 0
        self.consecutive_failures = 0
        self.skipped_unhealthy = 0
        self.last_error: Optional[str] = None

    @classmethod
    def from_env(cls) -> "DeadMansSwitch":
        def num(name, default):
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return float(default)
        return cls(os.environ.get("LIP_DEADMAN_URL") or None,
                   interval_s=num("LIP_DEADMAN_INTERVAL_S", 60.0), timeout_s=num("LIP_DEADMAN_TIMEOUT_S", 5.0))

    @property
    def configured(self) -> bool:
        return self._url is not None

    def ping(self, now: Optional[float] = None, *, healthy: bool = True) -> dict:
        now = time.time() if now is None else float(now)
        if not self.configured:
            return {"sent": False, "reason": "not_configured"}
        if not healthy:
            self.skipped_unhealthy += 1
            return {"sent": False, "reason": "unhealthy"}
        if self._last_attempt is not None and now - self._last_attempt < self.interval_s:
            return {"sent": False, "reason": "not_due"}
        self._last_attempt = now
        try:
            self._fetch(self._url, self.timeout_s)
        except Exception as exc:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = type(exc).__name__          # never str(exc): it may contain the URL
            return {"sent": False, "reason": "error", "error": self.last_error}
        self.last_ok_ts = now
        self.consecutive_failures = 0
        self.last_error = None
        return {"sent": True}

    def status(self) -> dict:
        return {"configured": self.configured, "interval_s": self.interval_s, "last_ok_ts": self.last_ok_ts,
                "failures": self.failures, "consecutive_failures": self.consecutive_failures,
                "skipped_unhealthy": self.skipped_unhealthy, "last_error": self.last_error}
