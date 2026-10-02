"""INNAIT alerts — centralized critical-event logging.

Writes one line per alert (``ISO  LEVEL  source  message``) to the engine
alert log (``alert_path()``):

* ``LIP_ENGINE_ALERT_LOG`` when set;
* else /var/lib/lip-maker/alerts-engine.log when that file can be written
  (it exists and is a writable regular file, or it does not exist yet and
  /var/lib/lip-maker is a writable directory; outside /opt/lip-maker, which
  deploy.sh moves aside on every deploy; not the watchdog's JSON-lines
  alerts.log in the same dir);
* else <repo>/logs/alerts.log (dev boxes, tests).

If the chosen file cannot be written the alert falls back to
<repo>/logs/alerts.log, and failing that it only goes to the logger. The
`innait_status.py` tool surfaces recent alerts in its output so nothing
important is missed.

Alert levels:
  CRITICAL — circuit breaker tripped, service halt, large unexpected loss
  WARN     — ramp retreat, blacklist addition, unusual scoring pattern
  INFO     — phase advance, daily P&L summary, period reconciliation

Callable from anywhere:
    from monitor.alerts import alert
    alert("CRITICAL", "lip_maker", "daily loss halt triggered: -$275")
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)
DEFAULT_ENGINE_ALERT_LOG = "/var/lib/lip-maker/alerts-engine.log"
FALLBACK_ALERT_LOG = str(Path(settings.LOGS_DIR) / "alerts.log")
_ALERT_PATH = Path(FALLBACK_ALERT_LOG)  # legacy name: the repo-tree fallback


def alert_path() -> Path:
    """Where alerts are written now (see the module docstring)."""
    env = os.environ.get("LIP_ENGINE_ALERT_LOG")
    if env:
        return Path(env)
    default = Path(DEFAULT_ENGINE_ALERT_LOG)
    if _writable(default):
        return default
    return Path(FALLBACK_ALERT_LOG)


def _writable(path: Path) -> bool:
    """The file itself when it exists (a root-owned log in a lip-owned dir is
    not writable for the engine), else its directory."""
    if path.exists():
        return path.is_file() and os.access(path, os.W_OK)
    return path.parent.is_dir() and os.access(path.parent, os.W_OK)


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(line + "\n")


def alert(level: str, source: str, message: str) -> None:
    """Log an alert to the engine alert log (``alert_path()``) and the
    current logger."""
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts}  {level:8s}  {source:12s}  {message}"
    path = alert_path()
    try:
        _append(path, line)
    except Exception as e:
        fallback = Path(FALLBACK_ALERT_LOG)
        _log.warning(f"alert-file write failed ({path}): {e}")
        if fallback != path:
            try:
                _append(fallback, line)
            except Exception as e2:
                _log.warning(f"alert-file fallback write failed ({fallback}): {e2}")
    # Always print to stdout/stderr so systemd journal captures it too
    if level == "CRITICAL":
        _log.error(line)
    elif level == "WARN":
        _log.warning(line)
    else:
        _log.info(line)


def recent_alerts(since_hours: int = 24, max_n: int = 50) -> list[str]:
    """Return the last N alerts within since_hours for dashboard display."""
    path = alert_path()
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
    out = []
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                ts_s = line.split("  ", 1)[0]
                try:
                    ts = datetime.fromisoformat(ts_s).timestamp()
                except ValueError:
                    continue
                if ts >= cutoff:
                    out.append(line.rstrip())
    except Exception:
        return []
    return out[-max_n:]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    alert("INFO", "alerts_self_test", "alerts module initialized")
    for l in recent_alerts():
        print(l)
