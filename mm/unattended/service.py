"""Paper/demo unattended Kalshi loop.

The process cancels resting orders before it quotes, and it refuses a
production host. Live trading stays off unless a later, separate
acknowledgement says otherwise — this package does not give that
acknowledgement.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from mm.safety.supervisor import write_heartbeat
from mm.venues.kalshi_rest import PRODUCTION_HOSTS


class UnattendedRefused(RuntimeError):
    """This process will not start against production or with paper off."""


def assert_paper_demo(*, paper: bool, ws_url: str | None) -> None:
    if not paper:
        raise UnattendedRefused("unattended service is paper/demo only")
    if not ws_url:
        return
    host = (urlsplit(ws_url).hostname or "").lower()
    if host in PRODUCTION_HOSTS:
        raise UnattendedRefused(f"production host refused: {host}")


class UnattendedSession:
    """Cancel first, then quote. Startup and every restart use this order."""

    def __init__(self, adapter, open_orders, quote, on_cancel=None) -> None:
        self.adapter = adapter
        self.open_orders = list(open_orders)
        self.quote = quote
        self.on_cancel = on_cancel or (lambda _n: None)

    def start(self) -> None:
        cancelled = self.adapter.cancel_all(self.open_orders)
        self.on_cancel(cancelled)
        self.quote()


class CrashWatchdog:
    """Run a session again after it raises. Each start cancels before quoting."""

    def __init__(self, factory, max_restarts: int = 5) -> None:
        self.factory = factory
        self.max_restarts = int(max_restarts)

    def run(self):
        crashes = 0
        while True:
            session = self.factory()
            try:
                session.start()
                return session
            except Exception:
                crashes += 1
                if crashes > self.max_restarts:
                    raise


def main(argv: list[str] | None = None) -> int:
    """Heartbeat loop. Does not open a socket and does not send orders.

    Quoting is attached by an ``UnattendedSession`` in the paper runner.
    This entrypoint keeps the process up, records a startup cancel, and
    refuses production.
    """
    parser = argparse.ArgumentParser(description="Kalshi paper/demo unattended loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--heartbeat", default="/var/lib/lip-maker/heartbeat")
    parser.add_argument("--cancel-log", default="/var/lib/lip-maker/startup-cancel")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    paper = os.environ.get("LIP_PAPER", "true").lower() == "true"
    ws_url = os.environ.get("LIP_KALSHI_WS_URL") or None
    assert_paper_demo(paper=paper, ws_url=ws_url)
    log = Path(args.cancel_log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("cancel_all\n")
    write_heartbeat(args.heartbeat)
    if args.once:
        return 0
    while True:
        write_heartbeat(args.heartbeat)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
