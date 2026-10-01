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
                    stale_ms: float, now: float | None = None,
                    closes: dict[str, float] | None = None,
                    pull_before_s: float | None = None) -> int:
    """Return 0 when the heartbeat is fresh, 2 when this process should flatten.

    A market inside the close window is cancelled even when the heartbeat
    is fresh. That line is ``cancel TICKER``. A stale heartbeat is still
    ``cancel_all``.
    """
    from mm.session_gates import markets_to_cancel, pull_before_close_s

    moment = time.time() if now is None else float(now)
    window = pull_before_close_s() if pull_before_s is None else float(pull_before_s)
    due = markets_to_cancel(closes or {}, moment, pull_before_s=window)
    path = Path(heartbeat)
    stale = True
    if path.exists():
        try:
            ts = float(path.read_text(encoding="utf-8").strip())
        except ValueError:
            ts = None
        if ts is not None and (moment - ts) * 1000.0 <= float(stale_ms):
            stale = False
    if not stale and not due:
        return 0
    log = Path(cancel_log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        if stale:
            fh.write("cancel_all\n")
        for market in due:
            fh.write(f"cancel {market}\n")
    return 2 if stale else 0


def _load_closes(path: str) -> dict[str, float]:
    import json
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(market): float(close_ts) for market, close_ts in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi quote-loop supervisor")
    parser.add_argument("--heartbeat", required=True)
    parser.add_argument("--cancel-log", required=True)
    parser.add_argument("--stale-ms", type=float, default=3000)
    parser.add_argument("--closes", default="", help="JSON map of market to close unix time")
    parser.add_argument("--pull-before-min", type=float, default=None,
                        help="minutes before close to cancel (default 15)")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required; this process does not daemonize")
    from mm.session_gates import pull_before_close_s
    closes = _load_closes(args.closes) if args.closes else None
    window = None if args.pull_before_min is None else pull_before_close_s(args.pull_before_min)
    return supervisor_once(
        args.heartbeat, args.cancel_log, stale_ms=args.stale_ms,
        closes=closes, pull_before_s=window,
    )


if __name__ == "__main__":
    raise SystemExit(main())
