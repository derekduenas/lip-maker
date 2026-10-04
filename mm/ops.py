"""Startup checks, clock skew, rate-limit wait, and log files.

Secrets stay in the environment or in files the process opens. This module
does not read them into log lines. A PEM block on the command line is refused.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from mm.venues.base import backoff_seconds

SKEW_LIMIT_S = 2.0
_SECRET_MARKERS = ("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY")


class ConfigError(RuntimeError):
    """Startup configuration that this process will not run with."""


def clock_skew_seconds(local_ts: float, exchange_ts: float) -> float:
    return float(local_ts) - float(exchange_ts)


def skew_is_excessive(local_ts: float, exchange_ts: float, *,
                      limit_s: float = SKEW_LIMIT_S) -> bool:
    return abs(clock_skew_seconds(local_ts, exchange_ts)) > float(limit_s)


class ChronyProbe:
    """Optional ``chronyc tracking`` reader for /status (never required, never raises).

    ``offset_s`` is the system clock minus NTP time: positive = local clock
    fast. Cached ``ttl_s`` so a status refresh never spawns a process per
    second. ``runner(cmd, timeout)`` returns the command's stdout (injectable
    for tests)."""

    def __init__(self, runner=None, clock=None, ttl_s: float = 30.0, background: bool = False) -> None:
        import time as _time
        self.background = bool(background)
        self._thread = None
        self._runner = runner or self._run
        self._clock = clock or _time.time
        self.ttl_s = float(ttl_s)
        self._at = None
        self._last: dict = {"offset_s": None, "error": "not read yet", "ts": None}

    @staticmethod
    def _run(cmd, timeout):
        import subprocess
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout

    def read(self) -> dict:
        now = self._clock()
        if self._at is not None and now - self._at < self.ttl_s:
            return self._last
        if self.background:
            # Never block the caller (the engine reads this under its lock): refresh on a thread
            # and return the last value meanwhile.
            import threading
            if self._thread is None or not self._thread.is_alive():
                self._at = now
                self._thread = threading.Thread(target=self._refresh, args=(now,), name="lip-chrony", daemon=True)
                self._thread.start()
            return self._last
        self._at = now
        self._refresh(now)
        return self._last

    def _refresh(self, now) -> None:
        import re
        try:
            text = self._runner(["chronyc", "tracking"], 3.0)
            m = re.search(r"System time\s*:\s*([0-9.]+)\s+seconds\s+(fast|slow)", str(text))
            if not m:
                self._last = {"offset_s": None, "error": "unparsed chronyc output", "ts": now}
            else:
                val = float(m.group(1)) * (1.0 if m.group(2) == "fast" else -1.0)
                self._last = {"offset_s": val, "error": None, "ts": now}
        except Exception as exc:  # chronyc missing, timeout, non-zero exit
            self._last = {"offset_s": None, "error": f"{type(exc).__name__}: {str(exc)[:80]}", "ts": now}


def redact(text: str) -> str:
    """Hide key material if it ever reaches a log formatter."""
    out = text
    for marker in _SECRET_MARKERS:
        if marker in out:
            out = out.split(marker, 1)[0] + marker + " [redacted]"
    return out


def assert_no_secret_on_command_line(argv: list[str]) -> None:
    for arg in argv:
        for marker in _SECRET_MARKERS:
            if marker in arg:
                raise ConfigError("refusing a private key passed on the command line")
        if arg.startswith("-----"):
            raise ConfigError("refusing a private key passed on the command line")


def validate_config(*, paper: bool, ws_url: str | None = None,
                    argv: list[str] | None = None) -> None:
    """Reject a non-paper unattended start and a key pasted into argv.

    ``LIP_RAMP_PHASE`` is checked when settings is imported. This function
    is the entrypoint check for the paper flag and the command line.
    """
    assert_no_secret_on_command_line(list(argv or []))
    if os.environ.get("LIP_PAPER", "true").lower() not in ("1", "true", "yes", "on"):
        if paper is False:
            raise ConfigError("LIP_PAPER is off; this entrypoint stays paper")
    if ws_url and "api.elections.kalshi.com" in ws_url:
        raise ConfigError("production websocket host is not this entrypoint")


def configure_logging(path: str | None = None) -> logging.Logger:
    """Attach a rotating file when ``path`` is set. Never log the environment."""
    log = logging.getLogger("lip")
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if path:
        # LIP_LOG_MAX_MB x (LIP_LOG_BACKUPS + 1) of history. The old fixed
        # 1 MB x 5 held only ~3-4 h at the normal rate (minutes during a
        # selection burst), so the overnight 2026-10-02 outage had no engine
        # log left by morning.
        try:
            max_mb = max(1.0, float(os.environ.get("LIP_LOG_MAX_MB", "20")))
            backups = max(1, int(os.environ.get("LIP_LOG_BACKUPS", "10")))
        except ValueError:
            max_mb, backups = 20.0, 10
        handler = RotatingFileHandler(path, maxBytes=int(max_mb * 1_000_000), backupCount=backups)
        handler.setFormatter(formatter)
        handler.addFilter(lambda record: setattr(record, "msg", redact(str(record.msg))) or True)
        log.addHandler(handler)
    elif not log.handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        log.addHandler(stream)
    return log


__all__ = [
    "ConfigError", "SKEW_LIMIT_S", "backoff_seconds", "clock_skew_seconds",
    "configure_logging", "redact", "skew_is_excessive", "validate_config",
]
