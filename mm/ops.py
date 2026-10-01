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
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5)
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
