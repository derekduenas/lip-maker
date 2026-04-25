"""INNAIT alerts — centralized critical-event logging.

Writes to logs/alerts.log in a structured, greppable format. The `innait_status.py`
tool surfaces recent alerts in its output so nothing important is missed.

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
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)
_ALERT_PATH = Path(settings.LOGS_DIR) / "alerts.log"


def alert(level: str, source: str, message: str) -> None:
    """Log an alert to both logs/alerts.log and the current logger."""
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts}  {level:8s}  {source:12s}  {message}"
    try:
        _ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_ALERT_PATH, "a") as f:
            f.write(line + "\n")
    except Exception as e:
        _log.warning(f"alert-file write failed: {e}")
    # Always print to stdout/stderr so systemd journal captures it too
    if level == "CRITICAL":
        _log.error(line)
    elif level == "WARN":
        _log.warning(line)
    else:
        _log.info(line)


def recent_alerts(since_hours: int = 24, max_n: int = 50) -> list[str]:
    """Return the last N alerts within since_hours for dashboard display."""
    if not _ALERT_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
    out = []
    try:
        with open(_ALERT_PATH) as f:
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
