"""Paper/demo unattended Kalshi loop.

The process cancels resting orders before it quotes, and it refuses a
production host. ``--run`` is the continuous selector, sizer, quoter,
scorer, allocator, and risk loop. Live trading stays off.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from mm.ops import (
    ConfigError, assert_no_secret_on_command_line, configure_logging, validate_config,
)
from mm.safety.supervisor import write_heartbeat
from mm.unattended.health import render_daily_summary
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


def _paper_env() -> bool:
    return os.environ.get("LIP_PAPER", "true").strip().lower() in ("1", "true", "yes", "on")


def _write_run_outputs(args, report: dict, started: list | None = None) -> None:
    from mm.status_page import status_payload
    write_heartbeat(args.heartbeat)
    if args.summary:
        dest = Path(args.summary)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_daily_summary(
            day=str(report.get("day") or ""),
            fills=int(report.get("fills_n") or 0),
            pnl_usd=float(report.get("pnl_usd") or 0),
            rewards_usd=float(report.get("rewards_usd") or 0),
        ), encoding="utf-8")
    if args.report:
        dest = Path(args.report)
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = dict(report)
        body["status"] = status_payload(report)
        dest.write_text(json.dumps(body, default=str), encoding="utf-8")
    if args.status_port and started is not None:
        if not started:
            from mm.status_page import StatusPage
            box = {"report": report}
            page = StatusPage(lambda: box["report"], port=int(args.status_port))
            threading.Thread(target=page._httpd.serve_forever, daemon=True).start()
            started.append(box)
        else:
            started[0]["report"] = report


def main(argv: list[str] | None = None) -> int:
    """Heartbeat, one recorded cycle, or the continuous ``--run`` loop.

    ``--run`` quotes. Without ``--replay`` it opens the demo websocket
    when a key file exists, and otherwise stays up without connecting.
    A production host is refused. ``assert_paper_demo(paper=False)``
    still refuses; demo mode is a separate flag on ``--run`` only.
    """
    parser = argparse.ArgumentParser(description="Kalshi paper/demo unattended loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--heartbeat", default="/var/lib/lip-maker/heartbeat")
    parser.add_argument("--cancel-log", default="/var/lib/lip-maker/startup-cancel")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--cycle", default="", help="JSONL recording to run one paper cycle")
    parser.add_argument("--run", action="store_true", help="continuous select/quote/score loop")
    parser.add_argument("--replay", default="", help="recorded websocket JSONL for --run")
    parser.add_argument("--report", default="", help="where to write the cycle JSON")
    parser.add_argument("--summary", default="", help="daily summary path")
    parser.add_argument("--status-port", type=int, default=0, help="loopback status port")
    parser.add_argument("--select-every", type=float, default=600.0)
    parser.add_argument("--log-file", default="", help="rotating log file")
    args = parser.parse_args(argv)
    if args.replay:
        args.run = True
    paper = _paper_env()
    ws_url = os.environ.get("LIP_KALSHI_WS_URL") or None
    try:
        assert_no_secret_on_command_line(list(argv or []))
        if ws_url and "api.elections.kalshi.com" in ws_url:
            raise ConfigError("production websocket host is not this entrypoint")
        if not args.run:
            validate_config(paper=paper, ws_url=ws_url, argv=list(argv or []))
            assert_paper_demo(paper=paper, ws_url=ws_url)
        elif paper:
            validate_config(paper=True, ws_url=ws_url, argv=list(argv or []))
    except ConfigError as exc:
        raise UnattendedRefused(str(exc)) from exc
    if args.log_file:
        configure_logging(args.log_file)
    log = Path(args.cancel_log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("cancel_all\n")
    write_heartbeat(args.heartbeat)
    if args.cycle and not args.run:
        from mm.cycle import run_recording
        report = run_recording(args.cycle)
        dest = Path(args.report or str(Path(args.heartbeat).with_suffix(".json")))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(report, default=str), encoding="utf-8")
        return 0
    if args.run:
        from mm.unattended.loop import (
            resolve_mode, resolve_ws_url, run_recorded, socket_plan, waiting_report,
        )
        mode = resolve_mode()
        url = resolve_ws_url(ws_url)
        if mode == "paper":
            assert_paper_demo(paper=True, ws_url=url)
        else:
            from mm.unattended.loop import assert_demo_host
            assert_demo_host(url)
        started: list = []
        if args.replay:
            report = run_recorded(
                args.replay, mode=mode, select_every=args.select_every,
            )
            report["ws_url"] = url
            _write_run_outputs(args, report, started)
            return 0
        while True:
            plan = socket_plan(url)
            report = waiting_report(plan["url"])
            report["stage"] = plan["stage"]
            report["mode"] = mode
            report["paper"] = mode == "paper"
            report["demo"] = mode == "demo"
            _write_run_outputs(args, report, started)
            if plan["socket"]:
                from mm.unattended.loop import RunLoop, drive_socket
                loop = RunLoop(mode=mode, select_every=args.select_every)
                loop.socket_opened = True

                def _on_frame(msg, loop=loop):
                    loop.on_frame(msg)
                    write_heartbeat(args.heartbeat)

                import asyncio
                asyncio.run(drive_socket(plan["url"], _on_frame))
                report = loop.finish()
                report["socket_opened"] = True
                report["ws_url"] = plan["url"]
                _write_run_outputs(args, report, started)
            if args.once:
                return 0
            time.sleep(args.interval)
    if args.once:
        return 0
    while True:
        write_heartbeat(args.heartbeat)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
