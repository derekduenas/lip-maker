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
    ``estimate_every_s`` (default 60), only for markets that were quoted,
    in batches of LIP_STATUS_EST_BATCH (25) markets per ``step`` (the timer
    steps once a second under the loop lock), so one refresh never holds the
    lock for the whole quoted set. ``build`` (under the lock) returns the
    report; ``publish`` writes it (no lock needed). Never calls
    ``loop.finish()``. A failure here logs and is swallowed so the feed
    keeps running.
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
        self._accrual = None
        self._est_todo = None
        self._est_new: dict = {}
        self._acc_new: dict = {}
        self._session_start = None
        self.refreshes = 0
        self.warning = None

    def step(self) -> None:
        """One bounded batch of the estimate refresh (call under the lock).
        A pass starts every ``estimate_every_s``; its results replace the
        previous ones when the whole quoted set is done."""
        now = self.clock()
        if self._est_todo is None:
            if self._last_est is not None and now - self._last_est < self.estimate_every_s:
                return
            quoted = (set(getattr(self.loop, "quoted_ever", ()) or ())
                      | {q["market"] for q in self.loop.quotes} | set(self.loop.resting))
            self._est_todo, self._est_new, self._acc_new = sorted(quoted), {}, {}
        try:
            n = max(1, int(float(os.environ.get("LIP_STATUS_EST_BATCH", 25))))
        except (TypeError, ValueError):
            n = 25
        batch, self._est_todo = self._est_todo[:n], self._est_todo[n:]
        if batch:
            self._est_new.update(self.loop.live_estimates(batch))
            self._acc_new.update(self.loop.live_accrual(batch))
        if not self._est_todo:
            self._estimates, self._accrual = self._est_new, self._acc_new
            self._est_todo = None
            self._last_est = now

    def build(self):
        """The status report when one is due, else None (call under the lock)."""
        now = self.clock()
        if self._session_start is None and self.loop.now:
            self._session_start = float(self.loop.now)
        try:
            self.step()
            if self._last is not None and now - self._last < self.every_s:
                return None
            self._last = now
            report = self.loop.live_snapshot(
                estimates=self._estimates, session_start_ts=self._session_start,
                accrual=self._accrual,
            )
            report["data_source"] = self.data_source
            report["ws_url"] = self.ws_url
            if self.warning:
                report["book_source_warning"] = self.warning
            return report
        except Exception:
            logging.getLogger("lip.status").exception("live status refresh failed")
            return None

    def publish(self, report) -> bool:
        """Write a ``build`` report (status page, summary, report file)."""
        if report is None:
            return False
        try:
            self.write(report)
            self.refreshes += 1
            return True
        except Exception:
            logging.getLogger("lip.status").exception("live status refresh failed")
            return False

    def maybe_refresh(self) -> bool:
        return self.publish(self.build())


def fv_samples_path() -> str:
    """LIP_FV_CALIB_SAMPLES_FILE: settled model fair-value samples (JSON
    lines, appended; read by tools/fit_fv_weather.py)."""
    return os.environ.get("LIP_FV_CALIB_SAMPLES_FILE", "/var/lib/lip-maker/fv_calib_samples.jsonl")


def _take_fv_samples(loop) -> list:
    """Settled calibration samples queued by the loop (call under loop.lock)."""
    cal = getattr(loop, "fv_calib", None)
    return cal.take_outbox() if cal is not None and hasattr(cal, "take_outbox") else []


def flush_fv_samples(loop, rows: list) -> bool:
    """Append ``rows`` to ``fv_samples_path()`` (outside loop.lock). On a
    write error the rows go back to the front of the loop's queue (bounded)
    for the next tick, and the error is logged."""
    if not rows:
        return True
    path = Path(fv_samples_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r, default=str, sort_keys=True) + "\n" for r in rows))
        return True
    except Exception:
        logging.getLogger("lip.status").exception("fv calibration samples write failed (%s)", path)
        from mm.unattended.fv_calib import OUTBOX_MAX
        with loop.lock:
            cal = loop.fv_calib
            keep = (list(rows) + cal.outbox)[-OUTBOX_MAX:]
            cal.outbox_dropped += len(rows) + len(cal.outbox) - len(keep)
            cal.outbox = keep
        return False


class EngineTimer:
    """Once a second, independent of market-data frames: honor the kill file,
    write the heartbeat, refresh the status page and persist engine state.

    Takes ``loop.lock`` (the lock the frame callback holds) for the kill
    file, the status snapshot and the state snapshot, so a kill-file cancel
    never races a frame; the heartbeat, status files and the state file
    (fsync) are written after the lock is released, so disk I/O never
    stalls the frame thread. If the frame thread wedges while holding the
    lock, ticks stop and the heartbeat goes stale (the watchdog notices)."""

    def __init__(self, loop, *, heartbeat: str, kill_path: str, refresher=None,
                 every_s: float = 1.0) -> None:
        self.loop = loop
        self.heartbeat = heartbeat
        self.kill_path = kill_path
        self.refresher = refresher
        self.every_s = float(every_s)
        self._stop = threading.Event()
        self._thread = None
        self.ticks = 0

    def tick(self) -> None:
        loop = self.loop
        with loop.lock:
            _honor_kill_file(loop, self.kill_path)
            report = self.refresher.build() if self.refresher is not None else None
            body = loop.state_snapshot(every_s=5.0)
            samples = _take_fv_samples(loop)
        write_heartbeat(self.heartbeat)
        flush_fv_samples(loop, samples)
        if report is not None:
            self.refresher.publish(report)
        if body is not None:
            try:
                loop.write_state(body)
            except Exception as exc:
                with loop.lock:
                    loop.note_state_save_failed(exc)
        self.ticks += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logging.getLogger("lip.status").exception("engine timer tick failed")
            self._stop.wait(self.every_s)

    def start(self) -> "EngineTimer":
        self._thread = threading.Thread(target=self._run, name="lip-engine-timer", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


class _Engine:
    """The one RunLoop of a ``--run`` process plus its background helpers."""

    def __init__(self, args, mode: str, books: dict, plan: dict, started: list) -> None:
        from mm.bankroll import capital_usd
        from mm.unattended.loop import RunLoop
        self.args = args
        self.books = books
        self.plan = plan
        self.started = started
        loop = RunLoop(
            mode=mode, select_every=args.select_every,
            bankroll=float(capital_usd()),
            first_select_warmup_s=float(os.environ.get("LIP_FIRST_SELECT_WARMUP_S", "60")),
            carry_forward=True,
        )
        loop.socket_opened = True
        loop.check_watchdog_capital()
        loop.attach_state(os.environ.get("LIP_STATE_FILE", "/var/lib/lip-maker/engine_state.json"))
        self.loop = loop
        from mm.unattended.fairvalue import FairValueCache, enabled as _fv_enabled
        if _fv_enabled():  # Patch 16: external fair value, background refresh only
            loop.fv = FairValueCache()
            loop.fv.start(loop.fv_cache_targets)
        from mm.unattended import bookrec as _bookrec
        self.rec = None
        if _bookrec.enabled():  # Patch 19: bounded compressed frame recorder
            try:
                self.rec = _bookrec.FrameRecorder().start()
                loop.recorder = self.rec
            except Exception:
                logging.getLogger("lip.recorder").exception("recorder start failed")
                self.rec = None
        if getattr(loop, "pmus", None) is None:  # Patch 21: PM US feed into this RunLoop
            try:
                from mm.unattended import pmus_paper as _pmp
                if _pmp.enabled() and mode == "paper":
                    loop.pmus = _pmp.PMUSFeed(loop).start()
            except Exception:
                logging.getLogger("lip.pmus").exception("pmus feed start failed")
        self.refresher = LiveStatusRefresher(
            loop, lambda rep: _write_run_outputs(args, rep, started),
            data_source=books["flag"], ws_url=plan["url"],
        )
        self.refresher.warning = book_source_warning(books, mode)
        self.timer = EngineTimer(
            loop, heartbeat=args.heartbeat,
            kill_path=os.environ.get("LIP_KILL_FILE", "/var/lib/lip-maker/KILL"),
            refresher=self.refresher,
        )
        self.timer.tick()  # kill file + heartbeat before the first frame
        self.timer.start()

    def on_frame(self, msg: dict) -> None:
        loop = self.loop
        with loop.lock:
            if self.rec is not None:
                self.rec.record(msg)
            loop.on_frame(msg)
            if getattr(loop, "pmus", None) is not None:
                loop.drain_external()  # Patch 21: PM US frames, same thread
        # Status refresh and state saves run on the EngineTimer thread.

    def settle_candidates(self) -> list:
        """Kalshi markets due a settlement check: held positions first (the
        loop's ``settle_view``), then markets with pending fair-value
        calibration samples past close (``fv_settle_view``), so samples whose
        settlement the socket missed still get scored. Both are immutable
        tuples replaced whole: no lock needed."""
        held = [m for m, venue in self.loop.settle_view if venue == "kalshi"]
        return list(dict.fromkeys(held + list(getattr(self.loop, "fv_settle_view", ()))))

    def mark_down(self, reason: str) -> None:
        self.down_since = time.time()
        self.on_frame({"kind": "disconnect", "ts": self.down_since, "reason": reason})

    def reconnect_if_down(self) -> None:
        """A new driver session after the previous one ended: the risk engine
        sees the gap (the read-only driver reports its own reconnects)."""
        if getattr(self, "down_since", None) is None:
            return
        now = time.time()
        self.on_frame({"kind": "reconnect", "ts": now, "stale_s": now - self.down_since})
        self.down_since = None

    def write_final(self) -> None:
        with self.loop.lock:
            report = self.loop.finish()
            report["socket_opened"] = True
            report["ws_url"] = self.plan["url"]
            report["data_source"] = self.books["flag"]
            _write_run_outputs(self.args, report, self.started)

    def stop(self) -> None:
        self.timer.stop()
        try:
            with self.loop.lock:
                samples = _take_fv_samples(self.loop)
            flush_fv_samples(self.loop, samples)
        except Exception:
            logging.getLogger("lip.status").exception("final fv samples flush failed")
        for bg in (getattr(self.loop, "pmus", None), getattr(self.loop, "fv", None), self.rec):
            try:
                if bg is not None and hasattr(bg, "stop"):
                    bg.stop()
            except Exception:
                logging.getLogger("lip.status").exception("background stop failed")
        try:
            with self.loop.lock:
                self.loop.save_state(force=True)
        except Exception:
            logging.getLogger("lip.status").exception("final engine state save failed")


def reset_state_kill(path: str) -> int:
    """Operator reset of a kill latch persisted in the engine state file.
    Positions and aggregates are kept. An unreadable file is not touched
    (move it aside to start flat)."""
    p = Path(path)
    if not p.exists():
        print(f"no engine state at {path}; nothing to reset")
        return 0
    data = json.loads(p.read_text(encoding="utf-8"))
    prev = data.get("kill")
    data["kill"] = None
    tmp = p.with_name(p.name + ".tmp.reset")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    os.replace(tmp, p)
    print(f"cleared engine kill latch in {path} (was: {prev!r})")
    return 0


def book_source_warning(books: dict, mode: str) -> str | None:
    """Paper mode reading DEMO books because the production read key is
    not in this process's environment: say so (startup log and status)."""
    if mode != "paper" or books.get("reader"):
        return None
    return ("paper engine is reading DEMO books (results not representative): "
            "KALSHI_PROD_READ_KEY_ID / KALSHI_PROD_READ_KEY_PATH are not set in the service "
            "environment or the key file is not readable. Set them in /etc/lip-maker/lip-maker.env "
            "(the repo .env is not loaded) and restart.")


def _paper_env() -> bool:
    return os.environ.get("LIP_PAPER", "true").strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


class SummaryHistory:
    """``<summary>.history.jsonl``: one JSON line per UTC day with that
    day's last daily summary (day, data_source(s), fills, P&L fields,
    attribution), so the readiness report can count days although the
    summary file itself is overwritten with the current day.

    A day's line is written when the next day's first report arrives (day
    roll), on ``flush`` (clean shutdown) and, so a SIGTERM/crash loses at
    most that much, every LIP_SUMMARY_HISTORY_EVERY_S (600 s; <= 0 off).
    Writing replaces that day's line (restarts keep one line per day) and
    keeps every data_source seen for the day in ``data_sources``. The file
    keeps the newest LIP_SUMMARY_HISTORY_MAX (400) days and is rewritten
    atomically (tmp + rename). Unparseable lines are dropped. Errors are
    logged, never raised: the history must not stop the summary."""

    def __init__(self, path: str, *, every_s: float | None = None, clock=time.time) -> None:
        self.path = Path(path)
        self.every_s = (_env_float("LIP_SUMMARY_HISTORY_EVERY_S", 600.0) if every_s is None
                        else float(every_s))
        self.clock = clock
        self.current: dict | None = None
        self.sources: set = set()
        self._written_at = clock()
        self._dirty = False

    def note(self, row: dict) -> None:
        day = str(row.get("day") or "")
        if not day:
            return
        if self.current is not None and self.current["day"] != day:
            self._write(self.current, self.sources)       # final line of the previous day
            self.sources = set()
        self.current = dict(row, day=day)
        if row.get("data_source"):
            self.sources.add(str(row["data_source"]))
        self._dirty = True
        if self.every_s > 0 and self.clock() - self._written_at >= self.every_s:
            self._write(self.current, self.sources)

    def flush(self) -> None:
        if self.current is not None and self._dirty:
            self._write(self.current, self.sources)

    def _write(self, row: dict, sources: set) -> None:
        self._written_at = self.clock()
        if row is self.current:
            self._dirty = False
        try:
            keep = max(1, int(_env_float("LIP_SUMMARY_HISTORY_MAX", 400)))
            days: dict = {}
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        old = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(old, dict) and old.get("day"):
                        days[str(old["day"])] = old
            prev = days.get(row["day"]) or {}
            merged = set(sources) | {str(x) for x in prev.get("data_sources") or []}
            if prev.get("data_source"):
                merged.add(str(prev["data_source"]))
            days[row["day"]] = dict(row, data_sources=sorted(merged), written_ts=time.time())
            body = "".join(json.dumps(days[d], default=str, sort_keys=True) + "\n"
                           for d in sorted(days)[-keep:])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logging.getLogger("lip.status").exception("daily summary history write failed (%s)", self.path)


_SUMMARY_HISTORIES: dict[str, SummaryHistory] = {}


def summary_history_path(summary: str) -> str:
    return str(summary) + ".history.jsonl"


def flush_summary_histories() -> None:
    """Write the current day's line of every summary history (shutdown)."""
    for hist in list(_SUMMARY_HISTORIES.values()):
        hist.flush()


def _summary_row(report: dict) -> dict:
    return {"day": str(report.get("day") or ""),
            "data_source": str(report.get("data_source") or "") or None,
            "fills": int(report.get("fills_n") or 0),
            "pnl_usd": float(report.get("pnl_usd") or 0),
            "rewards_usd": float(report.get("rewards_usd") or 0),
            "premium_paid_usd": (None if report.get("premium_paid_usd") is None
                                 else float(report["premium_paid_usd"])),
            "attribution": report.get("pnl_attribution")}


def _write_run_outputs(args, report: dict, started: list | None = None) -> None:
    from mm.status_page import status_payload
    write_heartbeat(args.heartbeat)
    if args.summary:
        hpath = summary_history_path(args.summary)
        hist = _SUMMARY_HISTORIES.get(hpath)
        if hist is None:
            hist = _SUMMARY_HISTORIES[hpath] = SummaryHistory(hpath)
        hist.note(_summary_row(report))
        dest = Path(args.summary)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_daily_summary(
            day=str(report.get("day") or ""),
            fills=int(report.get("fills_n") or 0),
            pnl_usd=float(report.get("pnl_usd") or 0),
            rewards_usd=float(report.get("rewards_usd") or 0),
            data_source=str(report.get("data_source") or "") or None,
            buckets=report.get("buckets"),
            premium_paid_usd=(None if report.get("premium_paid_usd") is None
                              else float(report["premium_paid_usd"])),
            attribution=report.get("pnl_attribution"),
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
    parser.add_argument("--reset-kill", action="store_true",
                        help="clear the engine kill latch saved in LIP_STATE_FILE (positions kept), then exit")
    args = parser.parse_args(argv)
    if args.reset_kill:
        return reset_state_kill(os.environ.get("LIP_STATE_FILE", "/var/lib/lip-maker/engine_state.json"))
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
    if not args.run:
        # --run writes its first heartbeat only after resolve_mode() and the
        # host checks pass: a refused start must not look alive.
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
        warning = book_source_warning(books, mode)
        if warning:
            logging.getLogger("lip.unattended").warning("!!! %s", warning)
        write_heartbeat(args.heartbeat)
        started: list = []
        if args.replay:
            report = run_recorded(
                args.replay, mode=mode, select_every=args.select_every,
            )
            report["ws_url"] = books["ws_url"] if books["reader"] else url
            report["data_source"] = books["flag"]
            _write_run_outputs(args, report, started)
            flush_summary_histories()
            return 0
        engine = None
        try:
            while True:
                if books["reader"]:
                    plan = {"socket": True, "url": books["ws_url"], "stage": "connect", "reader": True}
                else:
                    plan = socket_plan(url)
                    plan["reader"] = False
                if engine is None:
                    report = waiting_report(plan["url"])
                    report["stage"] = plan["stage"]
                    report["mode"] = mode
                    report["paper"] = mode == "paper"
                    report["demo"] = mode == "demo"
                    report["data_source"] = books["flag"]
                    if warning:
                        report["book_source_warning"] = warning
                    _write_run_outputs(args, report, started)
                if plan["socket"]:
                    if engine is None:
                        # One RunLoop for the life of the process: reconnects
                        # (including a clean 1000/1001 close) keep positions,
                        # cooldowns and the kill latch.
                        engine = _Engine(args, mode, books, plan, started)
                    import asyncio
                    from mm.unattended.loop import drive_readonly_books, drive_socket
                    engine.reconnect_if_down()
                    try:
                        if plan.get("reader"):
                            asyncio.run(drive_readonly_books(books, engine.on_frame,
                                                             settle_candidates=engine.settle_candidates))
                        else:
                            asyncio.run(drive_socket(plan["url"], engine.on_frame))
                    finally:
                        engine.mark_down("session_end")
                    if args.once:
                        engine.write_final()
                if args.once:
                    return 0
                time.sleep(args.interval)
        finally:
            if engine is not None:
                engine.stop()
            # Clean shutdown: the current day's summary goes to the history
            # (a SIGTERM bypasses this; SummaryHistory also checkpoints).
            flush_summary_histories()
    if args.once:
        return 0
    while True:
        write_heartbeat(args.heartbeat)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
