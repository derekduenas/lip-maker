"""Local JSON status. Binds to loopback only."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def _count(report: dict, number_key: str, value_key: str) -> int:
    numbered = report.get(number_key)
    if isinstance(numbered, bool):
        numbered = None
    if isinstance(numbered, (int, float)):
        return int(numbered)
    value = report.get(value_key)
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def status_payload(report: dict) -> dict:
    from mm.venues.readonly import book_source
    fills = report.get("fills_n")
    if not isinstance(fills, (int, float)) or isinstance(fills, bool):
        fills = _count(report, "fills_n", "fills")
    return {
        "paper": True,
        "live_armed": False,
        "stage": report.get("stage"),
        "markets": list(report.get("markets") or []),
        "programs_loaded": _count(report, "programs_loaded", "programs"),
        "selection_count": _count(report, "selection_count", "selection_count"),
        "quotes": _count(report, "quotes_n", "quotes"),
        "resting": _count(report, "resting_n", "resting"),
        "fills": int(fills),
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
