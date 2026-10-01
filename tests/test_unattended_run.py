"""Continuous run loop on a recorded websocket stream. No socket."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from engine.lip_reconcile import INFERRED_SOURCE, credits_from_ledger, infer_reward_credits
from engine.reward_provenance import PAID_SOURCES
from mm.compound import PRIOR_STRENGTH
from mm.session_gates import SeriesStats
from mm.unattended.loop import (
    DemoPoster, resolve_mode, resolve_ws_url, run_recorded, socket_plan,
)
from mm.unattended.service import UnattendedRefused, main

ROOT = Path(__file__).resolve().parents[1]
AUG = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
MARKET = "KXBRENT-26OCT07"


def _stream() -> list[dict]:
    def snap(ts, seq):
        return {
            "type": "orderbook_snapshot", "sid": 1, "seq": seq, "ts": ts,
            "msg": {
                "market_ticker": MARKET,
                "yes_dollars_fp": [["0.5000", "100.00"]],
                "no_dollars_fp": [["0.5000", "100.00"]],
            },
        }
    return [
        {
            "kind": "program", "market": MARKET, "series": "KXBRENT",
            "period_reward_usd": 86400, "period_seconds": 86400,
            "discount_factor": 0.5, "target_size": 100, "days_to_settle": 3,
            "exchange_index": 2, "shard_cash_usd": 10000,
            "start_ts": AUG, "end_ts": AUG + 86400, "close_ts": AUG + 20 * 60,
        },
        snap(AUG + 0.2, 1),
        snap(AUG + 1.2, 2),
        {"type": "clock", "ts": AUG + 2.0},
        {
            "type": "trade", "ts": AUG + 2.5,
            "trade": {
                "trade_id": "t-yes", "ticker": MARKET, "count_fp": "150.00",
                "yes_price_dollars": "0.50", "no_price_dollars": "0.50",
                "taker_side": "no", "created_time": "2026-08-01T00:00:02.500000+00:00",
            },
        },
        {
            "kind": "cash", "balance_delta_usd": "-23", "fills_cash_usd": "-25",
            "settlements_usd": "0", "deposits_usd": "0",
        },
        {"type": "clock", "ts": AUG + 600.2},
    ]


def test_infer_residual_is_marked_and_stays_out_of_paid_sources():
    assert INFERRED_SOURCE not in PAID_SOURCES
    none = infer_reward_credits(
        balance_delta_usd=0, fills_cash_usd=0, settlements_usd=0, deposits_usd=0,
        shares={"M": Decimal("1")},
    )
    assert none["credits"] == []
    assert none["inferred"] is True
    negative = infer_reward_credits(
        balance_delta_usd="-1", fills_cash_usd="0", settlements_usd="0", deposits_usd="0",
        shares={"M": Decimal("1")},
    )
    assert negative["credits"] == []
    split = infer_reward_credits(
        balance_delta_usd="10", fills_cash_usd="5", settlements_usd="1", deposits_usd="1",
        shares={"A": Decimal("1"), "B": Decimal("3")},
        series_by_market={"A": "S1", "B": "S2"},
    )
    assert split["confirmed"] is False
    assert Decimal(split["residual_usd"]) == Decimal("3")
    by_market = {row["market"]: Decimal(row["amount_usd"]) for row in split["credits"]}
    assert by_market["A"] == Decimal("0.75")
    assert by_market["B"] == Decimal("2.25")
    assert {row["source"] for row in split["credits"]} == {INFERRED_SOURCE}
    assert all(row["inferred"] is True for row in split["credits"])
    accepted, rejected = credits_from_ledger([{
        "kind": "balance", "source": INFERRED_SOURCE, "market": "A",
        "program_id": "A", "amount_usd": "3",
    }])
    assert accepted == []
    assert rejected


def test_modes_and_demo_poster():
    assert resolve_mode({"LIP_PAPER": "true", "LIP_DEMO": "true"}) == "paper"
    assert resolve_mode({"LIP_PAPER": "false", "LIP_DEMO": "true"}) == "demo"
    with pytest.raises(UnattendedRefused):
        resolve_mode({"LIP_PAPER": "false", "LIP_DEMO": "false"})
    with pytest.raises(UnattendedRefused):
        resolve_ws_url("wss://api.elections.kalshi.com/trade-api/ws/v2")
    assert resolve_ws_url(None).startswith("wss://demo-api.kalshi.co/")
    sent = []
    poster = DemoPoster("demo-api.kalshi.co", lambda body: sent.append(body) or body)
    body = poster.place(market="M", side="yes", price_cents=50, size=1, opposing_bid_cents=0)
    assert body["post_only"] is True
    assert sent[0]["post_only"] is True
    assert sent[0]["side"] == "bid"
    with pytest.raises(UnattendedRefused):
        DemoPoster("api.elections.kalshi.com", lambda body: body)
    plan = socket_plan("wss://demo-api.kalshi.co/trade-api/ws/v2", key_path="/nonexistent/key.pem")
    assert plan["socket"] is False
    assert plan["stage"] == "waiting_for_demo_key"


def test_run_loop_on_a_recorded_stream(tmp_path, monkeypatch):
    monkeypatch.delenv("KALSHI_PROD_READ_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PROD_READ_KEY_PATH", raising=False)
    path = tmp_path / "stream.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in _stream()) + "\n", encoding="utf-8")
    called = []
    report = run_recorded(
        path, mode="paper", bankroll=10_000, select_every=600,
        poster=lambda body: called.append(body),
    )
    assert report["mode"] == "paper"
    assert report["paper"] is True
    assert report["live_armed"] is False
    assert report["socket_opened"] is False
    assert called == []
    assert report["selection_count"] >= 2
    assert report["quotes"]
    quote = report["quotes"][0]
    assert quote["market"] == MARKET
    assert quote["paper"] is True
    assert quote["yes_cents"] == 50
    assert quote["size"] == 100
    assert quote["size"] <= 200
    assert report["fills"]
    assert report["fills"][0]["side"] == "yes"
    assert report["fills"][0]["count"] == 50
    assert any(row["reason"] == "close_cutoff" for row in report["cancelled"])
    assert report["resting"] == []
    assert Decimal(report["estimated_usd"]) == Decimal("1")
    assert report["risk"][0]["allowed"] is True
    assert report["kill"] is None
    assert report["inferred"]["inferred"] is True
    assert report["inferred"]["source"] == INFERRED_SOURCE
    assert report["inferred"]["credits"][0]["inferred"] is True
    assert Decimal(report["inferred"]["credits"][0]["amount_usd"]) == Decimal("2")
    assert report["calibration_inferred"] is True
    assert report["factors"]["KXBRENT"] == pytest.approx((2.0 + PRIOR_STRENGTH * 1.0) / (1 + PRIOR_STRENGTH))
    assert MARKET in report["next_usd"]
    out = tmp_path / "run.json"
    summary = tmp_path / "summary.txt"
    code = main([
        "--run", "--replay", str(path), "--once",
        "--report", str(out), "--summary", str(summary),
        "--heartbeat", str(tmp_path / "hb"),
        "--cancel-log", str(tmp_path / "cancel"),
    ])
    assert code == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["socket_opened"] is False
    assert saved["status"]["paper"] is True
    assert saved["status"]["live_armed"] is False
    assert "cancel_all" in (tmp_path / "cancel").read_text(encoding="utf-8")
    text = summary.read_text(encoding="utf-8")
    assert "fills 1" in text
    assert "data_source demo-books: results not representative" in text
    assert saved["status"]["data_source"] == "demo-books: results not representative"
    assert (tmp_path / "hb").exists()


def test_demo_mode_applies_the_series_gate(tmp_path):
    path = tmp_path / "stream.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in _stream()) + "\n", encoding="utf-8")
    blocked = run_recorded(path, mode="demo", bankroll=10_000, select_every=600)
    assert blocked["quotes"] == []
    assert any(why.startswith("series_gate:") for _market, why in blocked["excluded"])
    passed = SeriesStats("KXBRENT", days=5, settled_fills=30, net_usd=70, reward_usd=100, markout_5m_usd=30)
    sent = []
    poster = DemoPoster("demo-api.kalshi.co", lambda body: sent.append(body) or body)
    # The close frame is inside T-15, so a quote placed on the first book is
    # cancelled later. The sender still saw the post-only bodies.
    report = run_recorded(
        path, mode="demo", bankroll=10_000, select_every=10**9,
        poster=poster, series_stats={"KXBRENT": passed},
    )
    assert sent and all(body["post_only"] is True for body in sent)
    assert report["quotes"]
    assert report["quotes"][0]["paper"] is False


def test_run_without_a_key_does_not_open_a_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
    monkeypatch.delenv("LIP_KALSHI_WS_URL", raising=False)
    monkeypatch.delenv("KALSHI_PROD_READ_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PROD_READ_KEY_PATH", raising=False)
    monkeypatch.setenv("LIP_PAPER", "true")
    out = tmp_path / "idle.json"
    code = main([
        "--run", "--once",
        "--report", str(out),
        "--heartbeat", str(tmp_path / "hb"),
        "--cancel-log", str(tmp_path / "cancel"),
    ])
    assert code == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["socket_opened"] is False
    assert saved["stage"] == "waiting_for_demo_key"
    assert saved["ws_url"].startswith("wss://demo-api.kalshi.co/")
    assert saved["data_source"] == "demo-books: results not representative"
    assert saved["status"]["data_source"] == "demo-books: results not representative"


def test_unit_runs_the_paper_loop():
    unit = (ROOT / "deploy" / "lip-unattended.service").read_text(encoding="utf-8")
    script = (ROOT / "deploy" / "droplet" / "setup.sh").read_text(encoding="utf-8")
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "--run" in unit
    assert "wss://demo-api.kalshi.co/trade-api/ws/v2" in unit
    assert "LIP_PAPER=true" in unit
    assert "LIP_PAPER=false" not in unit
    assert "api.elections.kalshi.com" not in unit
    assert "--status-port 8765" in unit
    assert "/opt/lip-maker/.venv/bin/python" in unit
    assert "systemctl enable --now lip-unattended.service" in script
    assert "python3 -m venv /opt/lip-maker/.venv" in script
    assert "requirements.txt" in script
    assert "ufw already active with rules" in script
    for name in ("websockets", "requests", "cryptography", "certifi"):
        assert name in reqs


def test_prod_read_key_starts_and_survives_a_503(tmp_path, monkeypatch, caplog):
    """A production read key must start the loop, and a 503 must not exit 3."""
    import logging
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    caplog.set_level(logging.WARNING)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "read.pem"
    pem.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    monkeypatch.setenv("LIP_PAPER", "true")
    monkeypatch.delenv("LIP_DEMO", raising=False)
    monkeypatch.delenv("LIP_KALSHI_WS_URL", raising=False)
    monkeypatch.setenv("KALSHI_PROD_READ_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PROD_READ_KEY_PATH", str(pem))
    monkeypatch.setattr("mm.unattended.loop.READONLY_DATA_BACKOFF_S", 0.0)

    calls = []

    class Down:
        def request(self, method, url, headers=None, data=None, timeout=10):
            calls.append((method, url))
            return type("R", (), {"status_code": 503, "json": lambda self: {}})()

    monkeypatch.setattr("requests.Session", lambda: Down())
    out = tmp_path / "run.json"
    code = main([
        "--run", "--once",
        "--report", str(out),
        "--heartbeat", str(tmp_path / "hb"),
        "--cancel-log", str(tmp_path / "cancel"),
        "--summary", str(tmp_path / "summary"),
    ])
    assert code == 0
    assert calls and calls[0][0] == "GET"
    assert "incentive_programs" in calls[0][1]
    assert "503" in caplog.text
    assert "backing off" in caplog.text
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["data_source"] == "production-books"
    assert saved["paper"] is True
    assert saved["live_armed"] is False
    assert (tmp_path / "hb").exists()
    paper = (ROOT / "run_paper.py").read_text(encoding="utf-8")
    assert 'unattended_main(["--run"])' in paper


def test_dev_requirements_include_pytest_asyncio():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    script = (ROOT / "deploy" / "droplet" / "setup.sh").read_text(encoding="utf-8")
    assert "pytest-asyncio" not in runtime
    assert "pytest-asyncio" in dev
    assert "-r requirements.txt" in dev
    assert "requirements-dev.txt" not in script


def test_long_lived_socket_refreshes_status_without_finish(tmp_path, monkeypatch):
    """Frames on an open socket rewrite /status. finish() stays at session end."""
    monkeypatch.setenv("LIP_PAPER", "true")
    monkeypatch.delenv("LIP_DEMO", raising=False)
    monkeypatch.delenv("LIP_KALSHI_WS_URL", raising=False)
    monkeypatch.delenv("KALSHI_PROD_READ_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PROD_READ_KEY_PATH", raising=False)
    key = tmp_path / "demo.pem"
    key.write_text("not-a-key\n", encoding="utf-8")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key))
    clock = {"t": 0.0}
    monkeypatch.setattr("mm.unattended.service.time.monotonic", lambda: clock["t"])

    from mm.unattended import loop as loop_mod
    finish_calls = {"n": 0}
    real_finish = loop_mod.RunLoop.finish

    def counting_finish(self):
        finish_calls["n"] += 1
        return real_finish(self)

    monkeypatch.setattr(loop_mod.RunLoop, "finish", counting_finish)
    report = tmp_path / "report.json"
    summary = tmp_path / "summary.txt"
    seen = {}

    async def fake_drive(url, on_frame):
        waiting = json.loads(report.read_text(encoding="utf-8"))
        assert waiting["stage"] == "connect"
        assert waiting["markets"] == []
        assert waiting["status"]["stage"] == "connect"
        assert waiting["status"]["markets"] == []
        # Program, two books, a clock, and the at-price trade. Still before T-15.
        for row in _stream()[:5]:
            on_frame(row)
        clock["t"] = 10.0
        on_frame({"type": "clock", "ts": AUG + 3.0})
        assert finish_calls["n"] == 0
        seen["mid"] = json.loads(report.read_text(encoding="utf-8"))
        seen["summary"] = summary.read_text(encoding="utf-8")
        frozen = report.read_text(encoding="utf-8")
        clock["t"] = 15.0
        on_frame({"type": "clock", "ts": AUG + 4.0})
        assert report.read_text(encoding="utf-8") == frozen
        assert finish_calls["n"] == 0

    monkeypatch.setattr(loop_mod, "drive_socket", fake_drive)
    code = main([
        "--run", "--once",
        "--report", str(report),
        "--summary", str(summary),
        "--heartbeat", str(tmp_path / "hb"),
        "--cancel-log", str(tmp_path / "cancel"),
    ])
    assert code == 0
    assert finish_calls["n"] == 1
    mid = seen["mid"]
    assert mid["stage"] == "running"
    assert "inferred" not in mid
    assert "next_usd" not in mid
    assert mid["programs_loaded"] >= 1
    assert mid["selection_count"] >= 1
    assert MARKET in mid["markets"]
    assert mid["quotes"] and mid["quotes"][0]["paper"] is True
    assert MARKET in mid["resting"]
    assert mid["fills_n"] >= 1
    assert Decimal(mid["estimated_usd"]) >= 0
    assert mid["paper"] is True
    assert mid["live_armed"] is False
    status = mid["status"]
    assert status["stage"] == "running"
    assert status["paper"] is True
    assert status["live_armed"] is False
    assert status["programs_loaded"] >= 1
    assert status["selection_count"] >= 1
    assert MARKET in status["markets"]
    assert status["quotes"] >= 1
    assert status["resting"] >= 1
    assert status["fills"] >= 1
    assert Decimal(status["estimated_usd"]) >= 0
    assert "fills 1" in seen["summary"]
    assert "pnl_usd 0.0000" in seen["summary"]
    assert "rewards_usd 0.0000" in seen["summary"]


def test_shard_lookup_caches_and_waits(monkeypatch):
    from mm.unattended.loop import ShardLookup

    clock = {"t": 100.0}
    slept = []

    def _sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr("mm.unattended.loop.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("mm.unattended.loop.time.sleep", _sleep)
    lookup = ShardLookup(per_second=10)
    lookup.budget.tokens = 0
    calls = []

    class Reader:
        def get(self, path):
            calls.append(path)
            return {"market": {"exchange_index": 4, "ticker": path.rsplit("/", 1)[-1]}}

    assert lookup.exchange_index(Reader(), "MKT-A") == 4
    assert slept
    assert lookup.exchange_index(Reader(), "MKT-A") == 4
    assert calls == ["/markets/MKT-A"]


def test_prod_read_programs_are_paged_sharded_and_selected(tmp_path, monkeypatch):
    """A second incentive page with a real shard is quoted. Page one alone is not."""
    from datetime import timedelta
    from urllib.parse import parse_qs, urlsplit

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from mm.unattended.loop import reset_readonly_shard_cache

    reset_readonly_shard_cache()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "read.pem"
    pem.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    monkeypatch.setenv("LIP_PAPER", "true")
    monkeypatch.delenv("LIP_DEMO", raising=False)
    monkeypatch.delenv("LIP_KALSHI_WS_URL", raising=False)
    monkeypatch.setenv("KALSHI_PROD_READ_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PROD_READ_KEY_PATH", str(pem))

    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    end = (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    hourly = "KXTEMPH-26OCT01-B50"
    brent = "KXBRENT-26OCT07"

    def raw(ticker):
        return {
            "id": ticker,
            "market_ticker": ticker,
            "incentive_type": "liquidity",
            "period_reward": 864_000_000,
            "discount_factor_bps": 5000,
            "target_size_fp": "100.00",
            "start_date": start,
            "end_date": end,
            "paid_out": False,
        }

    calls = []

    class Books:
        def request(self, method, url, headers=None, data=None, timeout=10):
            calls.append((method, url))
            parts = urlsplit(url)
            query = parse_qs(parts.query)
            if parts.path.endswith("/incentive_programs"):
                if "cursor" not in query:
                    return type("R", (), {
                        "status_code": 200,
                        "json": lambda self: {
                            "incentive_programs": [raw(hourly)],
                            "next_cursor": "page-2",
                        },
                    })()
                assert query["cursor"] == ["page-2"]
                return type("R", (), {
                    "status_code": 200,
                    "json": lambda self: {
                        "incentive_programs": [raw(brent)],
                        "next_cursor": "",
                    },
                })()
            ticker = parts.path.rstrip("/").rsplit("/", 1)[-1]
            return type("R", (), {
                "status_code": 200,
                "json": lambda self, ticker=ticker: {
                    "market": {"ticker": ticker, "exchange_index": 2},
                },
            })()

    monkeypatch.setattr("requests.Session", lambda: Books())

    class FakeSocket:
        last = None

        def __init__(self, *, api_key, private_key, url):
            self.url = url
            self._ws = self
            self._done = False
            self.subscribed = None
            FakeSocket.last = self

        async def connect(self):
            return None

        async def subscribe(self, channels, tickers=None):
            self.subscribed = (list(channels), list(tickers or []))
            return {}

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return json.dumps({
                "type": "orderbook_snapshot", "sid": 1, "seq": 1, "ts": now.timestamp(),
                "msg": {
                    "market_ticker": brent,
                    "yes_dollars_fp": [["0.5000", "100.00"]],
                    "no_dollars_fp": [["0.5000", "100.00"]],
                },
            })

        async def close(self):
            return None

    monkeypatch.setattr("mm.venues.readonly.ReadOnlyMarketSocket", FakeSocket)
    out = tmp_path / "run.json"
    code = main([
        "--run", "--once",
        "--report", str(out),
        "--heartbeat", str(tmp_path / "hb"),
        "--cancel-log", str(tmp_path / "cancel"),
        "--summary", str(tmp_path / "summary"),
    ])
    assert code == 0
    urls = [url for method, url in calls if method == "GET"]
    pages = [url for url in urls if "incentive_programs" in url]
    assert len(pages) == 2
    assert "cursor=" not in pages[0]
    assert "cursor=page-2" in pages[1]
    assert sum(1 for url in urls if f"/markets/{brent}" in url) == 1
    assert sum(1 for url in urls if f"/markets/{hourly}" in url) == 1
    assert FakeSocket.last is not None
    assert brent in FakeSocket.last.subscribed[1]
    assert hourly in FakeSocket.last.subscribed[1]
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert hourly in saved["markets"]
    assert brent in saved["markets"]
    selected = [row["market"] for row in saved["quotes"]]
    assert selected
    assert brent in selected
    assert hourly not in selected
    assert brent in saved["resting"]
    assert saved["paper"] is True
    assert saved["live_armed"] is False
    assert saved["status"]["paper"] is True
    assert saved["status"]["mode"] == "paper"
    assert saved["status"]["live_armed"] is False
