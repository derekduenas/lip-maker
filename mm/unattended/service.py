"""Paper/demo unattended Kalshi loop.

The process cancels resting orders before it quotes, and it refuses a
production host. ``--run`` is the continuous selector, sizer, quoter,
scorer, allocator, and risk loop. Live trading stays off.
"""
from __future__ import annotations

import argparse
import json
import logging
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



def _honor_kill_file(loop, path) -> bool:
    """Patch 17: external kill flag (written by lip-watchdog). Fail closed."""
    try:
        if not path or not os.path.exists(path):
            return False
    except Exception:
        return False
    try:
        reason = str(json.loads(Path(path).read_text(encoding="utf-8")).get("reason") or "kill_file")
    except Exception:
        reason = "kill_file"
    loop.external_kill(reason[:200])
    return True

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


class LiveStatusRefresher:
    """Push a read-only RunLoop snapshot to the status page and daily summary.

    Counts refresh every ``every_s`` seconds (default 10). Per-market USD
    estimates are heavier (they walk scored seconds), so they refresh every
    ``estimate_every_s`` (default 60) and only for markets that were quoted.
    Never calls ``loop.finish()``. A failure here logs and is swallowed so
    the feed keeps running.
    """

    def __init__(self, loop, write, *, data_source: str, ws_url: str,
                 every_s: float = 10.0, estimate_every_s: float = 60.0,
                 clock=time.monotonic) -> None:
        self.loop = loop
        self.write = write
        self.data_source = data_source
        self.ws_url = ws_url
        self.every_s = float(every_s)
        self.estimate_every_s = float(estimate_every_s)
        self.clock = clock
        self._last = None
        self._last_est = None
        self._estimates = None
        self._session_start = None
        self.refreshes = 0

    def maybe_refresh(self) -> bool:
        now = self.clock()
        if self._session_start is None and self.loop.now:
            self._session_start = float(self.loop.now)
        if self._last is not None and now - self._last < self.every_s:
            return False
        self._last = now
        try:
            if self._last_est is None or now - self._last_est >= self.estimate_every_s:
                quoted = (set(getattr(self.loop, "quoted_ever", ()) or ())
                          | {q["market"] for q in self.loop.quotes} | set(self.loop.resting))
                self._estimates = self.loop.live_estimates(quoted)
                self._accrual = self.loop.live_accrual(quoted)
                self._last_est = now
            report = self.loop.live_snapshot(
                estimates=self._estimates, session_start_ts=self._session_start,
                accrual=getattr(self, "_accrual", None),
            )
            report["data_source"] = self.data_source
            report["ws_url"] = self.ws_url
            self.write(report)
            self.refreshes += 1
            return True
        except Exception:
            logging.getLogger("lip.status").exception("live status refresh failed")
            return False


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
            data_source=str(report.get("data_source") or "") or None,
            buckets=report.get("buckets"),
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
        from mm.venues.readonly import book_source
        mode = resolve_mode()
        url = resolve_ws_url(ws_url)
        if mode == "paper":
            assert_paper_demo(paper=True, ws_url=url)
            books = book_source()
        else:
            from mm.unattended.loop import assert_demo_host
            assert_demo_host(url)
            books = book_source(force_demo=True)
        started: list = []
        if args.replay:
            report = run_recorded(
                args.replay, mode=mode, select_every=args.select_every,
            )
            report["ws_url"] = books["ws_url"] if books["reader"] else url
            report["data_source"] = books["flag"]
            _write_run_outputs(args, report, started)
            return 0
        while True:
            if books["reader"]:
                plan = {"socket": True, "url": books["ws_url"], "stage": "connect", "reader": True}
            else:
                plan = socket_plan(url)
                plan["reader"] = False
            report = waiting_report(plan["url"])
            report["stage"] = plan["stage"]
            report["mode"] = mode
            report["paper"] = mode == "paper"
            report["demo"] = mode == "demo"
            report["data_source"] = books["flag"]
            _write_run_outputs(args, report, started)
            if plan["socket"]:
                from mm.unattended.loop import RunLoop, drive_readonly_books, drive_socket
                from mm.bankroll import capital_usd
                loop = RunLoop(
                    mode=mode, select_every=args.select_every,
                    bankroll=float(capital_usd()),
                    first_select_warmup_s=float(os.environ.get("LIP_FIRST_SELECT_WARMUP_S", "60")),
                    carry_forward=True,
                )
                loop.socket_opened = True
                from mm.unattended.fairvalue import FairValueCache, enabled as _fv_enabled
                if _fv_enabled():  # Patch 16: external fair value, background refresh only
                    loop.fv = FairValueCache()
                    loop.fv.start(lambda loop=loop: {m for m in (set(loop.resting.copy()) | loop._fv_wanted.copy())
                                                     if not m.startswith("PMUS:")})

                from mm.unattended import bookrec as _bookrec
                _rec = None
                if _bookrec.enabled():  # Patch 19: bounded compressed frame recorder
                    try:
                        _rec = _bookrec.FrameRecorder().start()
                        loop.recorder = _rec
                    except Exception:
                        logging.getLogger("lip.recorder").exception("recorder start failed")
                        _rec = None
                if getattr(loop, "pmus", None) is None:  # Patch 21: PM US feed into this RunLoop
                    try:
                        from mm.unattended import pmus_paper as _pmp
                        if _pmp.enabled() and mode == "paper":
                            loop.pmus = _pmp.PMUSFeed(loop).start()
                    except Exception:
                        logging.getLogger("lip.pmus").exception("pmus feed start failed")

                refresher = LiveStatusRefresher(
                    loop, lambda rep: _write_run_outputs(args, rep, started),
                    data_source=books["flag"], ws_url=plan["url"],
                )

                _hb = {"sec": None}
                _kill_path = os.environ.get("LIP_KILL_FILE", "/var/lib/lip-maker/KILL")

                def _on_frame(msg, loop=loop, refresher=refresher, rec=_rec):
                    if rec is not None:
                        rec.record(msg)
                    loop.on_frame(msg)
                    if getattr(loop, "pmus", None) is not None:
                        loop.drain_external()  # Patch 21: PM US frames, same thread
                    sec = int(time.time())
                    if _hb["sec"] != sec:  # heartbeat file write once per second
                        _hb["sec"] = sec
                        write_heartbeat(args.heartbeat)
                        _honor_kill_file(loop, _kill_path)
                    refresher.maybe_refresh()

                import asyncio
                try:
                    if plan.get("reader"):
                        asyncio.run(drive_readonly_books(books, _on_frame))
                    else:
                        asyncio.run(drive_socket(plan["url"], _on_frame))
                finally:
                    # Patch 21 (audit): background threads of this session must
                    # not outlive it (a new RunLoop starts its own).
                    for _bg in (getattr(loop, "pmus", None), getattr(loop, "fv", None), _rec):
                        try:
                            if _bg is not None and hasattr(_bg, "stop"):
                                _bg.stop()
                        except Exception:
                            logging.getLogger("lip.status").exception("background stop failed")
                report = loop.finish()
                report["socket_opened"] = True
                report["ws_url"] = plan["url"]
                report["data_source"] = books["flag"]
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
