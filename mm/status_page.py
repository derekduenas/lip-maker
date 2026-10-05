"""Local JSON status. Binds to loopback only."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


LIVE_STATUS_FIELDS = (
    "programs_loaded", "selection_count", "selected_n", "selected_top",
    "resting_n", "quotes_n", "cancels_n", "fills_n", "excluded_n",
    "estimates_partial", "session_elapsed_s", "last_frame_ts", "pnl_usd",
    "excluded_reasons", "programs_shard_unknown",
    "programs_fed", "screen", "suspect_n",
    "paper_capital_usd", "alloc_budget_usd", "bankroll_usd", "risk_limits",
    "cap_skips_n", "cap_skips", "rank_skips_n",
    "estimated_raw_usd", "estimated_usd_note", "accrual_seconds",
    "buckets", "durable_reserve",
    "fills_detail", "markouts", "policy_skips", "pulls", "repegs_n", "skew", "recorder", "pmus", "venues", "fair_value", "select_ms", "size_ladder",
    # Review fixes: honest P&L, rolled-over periods, feed state.
    "premium_paid_usd", "pnl_usd_note", "pnl_parts",
    "unsettled_positions", "unmarked_positions",
    "closed_periods_n", "closed_periods_raw_usd", "feed",
    # Review fixes: inventory risk, daily MTM, engine alerts, capital check.
    "daily_mtm_pnl_usd", "inventory_locked_usd", "engine_alerts", "cap_trims_n",
    "budget_warning",
    # Review fixes: program pruning, engine state file.
    "programs_pruned_n", "state",
    # Integration: per-market positions (watchdog inventory), synthetic fills.
    "positions", "fills_synthetic_n",
    # Phase 4 instrumentation (estimates, paper).
    "markout_horizons", "pnl_attribution", "event_calendar",
    # Final review: released unresolved positions, demo-books warning.
    "unresolved_positions", "book_source_warning",
    # Model fair-value scoring, lifetime settled counters, report day.
    "fv_calibration", "settled_positions_n", "settled_positions_by_venue",
    "settled_total_usd", "settled_positions_lower_bound",
    "day", "rewards_usd", "socket_opened",
    # 2026-10-04 fill fix: Oct 6 checkpoint inputs, fill sources, sampling group.
    "checkpoint", "fills_by_source", "fill_sampling", "clock_skew_pulls_24h",
    # Oct 10 go/no-go per Kalshi series (mm.session_gates.series_go).
    "series_gate", "capital", "rewards_reconciliation", "adverse_guard", "deadman", "build",
)


def status_payload(report: dict) -> dict:
    out = _base_status(report)
    for field in LIVE_STATUS_FIELDS:
        if field in report:
            out[field] = report[field]
    return out


def actual_mode(environ: dict | None = None) -> dict:
    """Mode from the real config, not a constant.

    ``live_armed`` follows ``config.settings``' rule: LIP_PAPER false AND
    LIP_LIVE_ACK equal to the acknowledgement phrase.
    """
    import os
    env = os.environ if environ is None else environ

    def _on(name: str, default: str) -> bool:
        return str(env.get(name, default)).strip().lower() in ("1", "true", "yes", "on")

    try:
        from config.settings import LIVE_ACK_PHRASE
    except Exception:
        LIVE_ACK_PHRASE = "I_ACCEPT_LIVE_RISK"
    paper = _on("LIP_PAPER", "true")
    demo = (not paper) and _on("LIP_DEMO", "false")
    live_armed = (not paper) and str(env.get("LIP_LIVE_ACK", "")) == LIVE_ACK_PHRASE
    mode = "paper" if paper else ("demo" if demo else ("live" if live_armed else "refused"))
    return {"paper": paper, "demo": demo, "live_armed": live_armed, "mode": mode}


def _base_status(report: dict) -> dict:
    from mm.venues.readonly import book_source
    real = actual_mode()
    # A report that claims paper while config says otherwise reports the config.
    paper = bool(real["paper"]) and report.get("paper", True) is not False
    live_armed = bool(real["live_armed"]) or report.get("live_armed") is True
    return {
        "paper": paper,
        "demo": bool(real["demo"]) or report.get("demo") is True,
        "live_armed": live_armed,
        "mode": report.get("mode") or real["mode"],
        "stage": report.get("stage"),
        "markets": report.get("markets") or [],
        "estimated_usd": report.get("estimated_usd"),
        "kill": report.get("kill"),
        "data_source": report.get("data_source") or book_source()["flag"],
    }


class StatusPage:
    def __init__(self, report_fn: Callable[[], dict], *, port: int = 8765) -> None:
        self.report_fn = report_fn
        self.port = int(port)
        payload_fn = report_fn

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.split("?", 1)[0] not in ("/", "/status"):
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(status_payload(payload_fn())).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = int(self._httpd.server_address[1])

    def serve_one(self) -> None:
        self._httpd.handle_request()

    def close(self) -> None:
        self._httpd.server_close()
