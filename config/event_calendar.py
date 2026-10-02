"""Scheduled-event calendar: windows in which the paper engine pulls and
refuses quotes on matching markets (reason ``scheduled_event``).

The operator maintains the file; this code does not know any real release
dates. ``LIP_EVENT_CALENDAR_FILE`` names a JSON file (or YAML, when PyYAML
is installed and the name ends in .yaml/.yml)::

    {
      "_comment": "keys starting with _ are ignored",
      "defaults": {"pre_minutes": 30, "post_minutes": 30},
      "events": [
        {"name": "...", "series_prefixes": ["KXCPI"],
         "at": "2099-01-15T13:30:00Z", "pre_minutes": 30, "post_minutes": 15}
      ]
    }

A market matches an event when its series or its ticker starts with one of
the event's prefixes (case-insensitive; "KXCPI" also matches "KXCPIYOY").
It is blocked from ``at - pre_minutes`` to ``at + post_minutes`` inclusive,
measured on the loop clock. ``at`` must carry a timezone (Z or an offset).

No file configured: nothing is pulled (logged once). A configured file that
is missing, unreadable or invalid fails closed: every market is blocked with
reason ``scheduled_event_calendar_error`` until the file is fixed. The file
is re-read when its mtime changes (checked at most every
``reload_every_s``, 60 s).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ENV = "LIP_EVENT_CALENDAR_FILE"
DEFAULT_PRE_MIN = 30.0
DEFAULT_POST_MIN = 30.0
REASON = "scheduled_event"
ERROR_REASON = "scheduled_event_calendar_error"

_log = logging.getLogger("lip.calendar")


@dataclass(frozen=True)
class ScheduledEvent:
    name: str
    prefixes: tuple
    at_ts: float
    start_ts: float
    end_ts: float


def _minutes(raw, default: float, what: str) -> float:
    if raw is None:
        return float(default)
    if isinstance(raw, bool):
        raise ValueError(f"{what} must be a number of minutes")
    val = float(raw)
    if not val >= 0 or val == float("inf"):
        raise ValueError(f"{what} must be >= 0 minutes")
    return val


def _ts(raw, where: str) -> float:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{where}: 'at' must be an ISO 8601 string")
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"{where}: 'at' has no timezone ({raw!r}); use Z or an offset")
    return dt.timestamp()


def parse_calendar(data) -> list[ScheduledEvent]:
    """Validate a decoded calendar; raises ValueError on any problem."""
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("calendar must be an object with an 'events' list")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("'defaults' must be an object")
    pre0 = _minutes(defaults.get("pre_minutes"), DEFAULT_PRE_MIN, "defaults.pre_minutes")
    post0 = _minutes(defaults.get("post_minutes"), DEFAULT_POST_MIN, "defaults.post_minutes")
    out = []
    for i, ev in enumerate(data["events"]):
        where = f"events[{i}]"
        if not isinstance(ev, dict):
            raise ValueError(f"{where} must be an object")
        prefixes = ev.get("series_prefixes")
        if (not isinstance(prefixes, list) or not prefixes
                or not all(isinstance(p, str) and p.strip() for p in prefixes)):
            raise ValueError(f"{where}: 'series_prefixes' must be a non-empty list of strings")
        at = _ts(ev.get("at"), where)
        pre = _minutes(ev.get("pre_minutes"), pre0, f"{where}.pre_minutes")
        post = _minutes(ev.get("post_minutes"), post0, f"{where}.post_minutes")
        out.append(ScheduledEvent(name=str(ev.get("name") or where),
                                  prefixes=tuple(p.strip().upper() for p in prefixes),
                                  at_ts=at, start_ts=at - pre * 60.0, end_ts=at + post * 60.0))
    return out


def _decode(path: str, text: str):
    if path.lower().endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:
            raise ValueError("YAML calendar needs PyYAML (not installed); use JSON") from exc
        return yaml.safe_load(text)
    return json.loads(text)


class EventCalendar:
    def __init__(self, path: str | None, *, clock=time.time, reload_every_s: float = 60.0) -> None:
        self.path = str(path).strip() if path and str(path).strip() else None
        self.clock = clock
        self.reload_every_s = float(reload_every_s)
        self.events: list[ScheduledEvent] = []
        self.error: str | None = None
        self._mtime = None
        self._checked_at = None
        self._logged_none = False
        self._logged_error = None
        if self.path:
            self._load()

    @classmethod
    def from_env(cls, environ=None) -> "EventCalendar":
        env = os.environ if environ is None else environ
        return cls(env.get(ENV))

    # ------------------------------------------------------------ loading
    def _fail(self, message: str) -> None:
        self.error = message[:300]
        self.events = []
        if self._logged_error != self.error:
            _log.error("scheduled-event calendar %s unusable (%s): every market is blocked "
                       "(%s) until it is fixed", self.path, self.error, ERROR_REASON)
            self._logged_error = self.error

    def _load(self) -> None:
        self._checked_at = self.clock()
        try:
            mtime = os.stat(self.path).st_mtime
            events = parse_calendar(_decode(self.path, Path(self.path).read_text(encoding="utf-8")))
        except Exception as exc:   # fail closed: any read/parse problem blocks quoting
            self._mtime = None
            self._fail(f"{type(exc).__name__}: {exc}")
            return
        self.events = events
        self.error = None
        self._logged_error = None
        self._mtime = mtime
        _log.info("scheduled-event calendar %s: %d events", self.path, len(events))

    def _maybe_reload(self) -> None:
        now = self.clock()
        if self._checked_at is not None and now - self._checked_at < self.reload_every_s:
            return
        self._checked_at = now
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError as exc:
            self._mtime = None
            self._fail(f"{type(exc).__name__}: {exc}")
            return
        if mtime != self._mtime:
            self._load()

    # ------------------------------------------------------------ queries
    def check(self, series: str, market: str, ts: float) -> str:
        """"" when quoting is allowed, else the block reason."""
        if not self.path:
            if not self._logged_none:
                _log.info("no scheduled-event calendar (%s unset): no scheduled-event pulls", ENV)
                self._logged_none = True
            return ""
        self._maybe_reload()
        if self.error is not None:
            return ERROR_REASON
        s, m = str(series or "").upper(), str(market or "").upper()
        for ev in self.events:
            if ev.start_ts <= ts <= ev.end_ts and any(s.startswith(p) or m.startswith(p) for p in ev.prefixes):
                return REASON
        return ""

    def active(self, ts: float) -> list[dict]:
        return [{"name": ev.name, "prefixes": list(ev.prefixes), "at_ts": ev.at_ts,
                 "start_ts": ev.start_ts, "end_ts": ev.end_ts}
                for ev in self.events if ev.start_ts <= ts <= ev.end_ts]

    def summary(self, ts: float | None = None) -> dict:
        return {"configured": self.path is not None, "path": self.path, "events_n": len(self.events),
                "error": self.error,
                "active": [] if ts is None else self.active(ts)}
