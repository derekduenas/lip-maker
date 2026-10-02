"""Final review deploy notes: a paper engine that falls back to DEMO books
(the production read key is missing from the service environment; the repo
.env is no longer auto-loaded) says so loudly at startup and in status, and
the APEX README carries a pre-deploy checklist."""
import json
import logging
from pathlib import Path

import pytest

from mm.unattended import service as S

README = Path(__file__).resolve().parent.parent / "deploy/apex/README.md"


def _run(tmp_path, monkeypatch, books):
    import mm.unattended.loop as loop_mod
    import mm.venues.readonly as R
    monkeypatch.setenv("LIP_PAPER", "true")
    monkeypatch.delenv("LIP_DEMO", raising=False)
    monkeypatch.setattr(R, "book_source", lambda force_demo=False, environ=None: dict(books))
    monkeypatch.setattr(loop_mod, "socket_plan", lambda url, **kw: {
        "socket": False, "stage": "waiting_for_demo_key", "url": url, "host": "demo-api.kalshi.co"})
    rep = tmp_path / "report.json"
    assert S.main(["--run", "--once", "--heartbeat", str(tmp_path / "hb"),
                   "--cancel-log", str(tmp_path / "cancel"), "--report", str(rep)]) == 0
    return json.loads(rep.read_text())


def test_demo_book_fallback_warns_at_startup_and_in_status(tmp_path, monkeypatch, caplog):
    from mm.venues.readonly import book_source
    books = book_source({})  # no KALSHI_PROD_READ_KEY_ID / _PATH in the environment
    assert books["reader"] is False
    with caplog.at_level(logging.WARNING):
        body = _run(tmp_path, monkeypatch, books)
    assert any("KALSHI_PROD_READ_KEY" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)
    assert "KALSHI_PROD_READ_KEY" in body["book_source_warning"]
    assert "KALSHI_PROD_READ_KEY" in body["status"]["book_source_warning"]


def test_engine_status_carries_the_warning(tmp_path, monkeypatch):
    from tests.test_review_loop_pnl import M, apply_policy, program
    apply_policy(monkeypatch)
    monkeypatch.setenv("LIP_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("LIP_KILL_FILE", str(tmp_path / "KILL"))
    import argparse
    args = argparse.Namespace(select_every=600.0, heartbeat=str(tmp_path / "hb"), summary="",
                              report=str(tmp_path / "r.json"), status_port=0)
    books = {"flag": "demo-books: results not representative", "reader": False, "ws_url": "wss://x"}
    eng = S._Engine(args, "paper", books, {"url": "wss://x"}, [])
    try:
        eng.on_frame(program(M))
        eng.refresher._last = None
        eng.timer.tick()
        body = json.loads((tmp_path / "r.json").read_text())
        assert "KALSHI_PROD_READ_KEY" in body["status"]["book_source_warning"]
    finally:
        eng.timer.stop()
        eng.stop()


def test_production_books_have_no_warning():
    assert S.book_source_warning({"reader": True}, "paper") is None


def test_readme_has_the_pre_deploy_checklist():
    text = README.read_text()
    assert "## Before deploying this branch" in text
    sec = text.split("## Before deploying this branch", 1)[1]
    for needle in ("LIP_BANKROLL", "KALSHI_PROD_READ_KEY_ID", "/etc/lip-maker/lip-maker.env",
                   "/opt/lip-maker/.env", "polkit", "$25", "engine_state.json", "--reset-kill",
                   "lip_watchdog --reset"):
        assert needle in sec, needle
