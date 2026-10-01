"""Out-of-process dead-man for the Kalshi quote loop.

The main loop writes a timestamp. This process reads it. A missing or
stale file appends ``cancel_all`` to a log and exits 2. The default
supervisor does not hold API keys and does not send. A deployment that
should flatten points the same decision at ``SafeSender.trigger_all``.

    python -m mm.safety.supervisor --heartbeat PATH --cancel-log PATH --stale-ms N --once
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path


def write_heartbeat(path: str | Path, now: float | None = None) -> None:
    ts = time.time() if now is None else float(now)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"{ts}\n", encoding="utf-8")


def supervisor_once(heartbeat: str | Path, cancel_log: str | Path, *,
                    stale_ms: float, now: float | None = None) -> int:
    """Return 0 when the heartbeat is fresh, 2 when this process should flatten."""
    moment = time.time() if now is None else float(now)
    path = Path(heartbeat)
    stale = True
    if path.exists():
        try:
            ts = float(path.read_text(encoding="utf-8").strip())
        except ValueError:
            ts = None
        if ts is not None and (moment - ts) * 1000.0 <= float(stale_ms):
            stale = False
    if not stale:
        return 0
    log = Path(cancel_log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("cancel_all\n")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi quote-loop supervisor")
    parser.add_argument("--heartbeat", required=True)
    parser.add_argument("--cancel-log", required=True)
    parser.add_argument("--stale-ms", type=float, default=3000)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required; this process does not daemonize")
    return supervisor_once(args.heartbeat, args.cancel_log, stale_ms=args.stale_ms)


if __name__ == "__main__":
    raise SystemExit(main())
