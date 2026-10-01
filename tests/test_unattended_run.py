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
    assert "--run" in unit
    assert "wss://demo-api.kalshi.co/trade-api/ws/v2" in unit
    assert "LIP_PAPER=true" in unit
    assert "LIP_PAPER=false" not in unit
    assert "api.elections.kalshi.com" not in unit
    assert "--status-port 8765" in unit
    assert "systemctl enable --now lip-unattended.service" in script
    paper = (ROOT / "run_paper.py").read_text(encoding="utf-8")
    assert 'unattended_main(["--run"])' in paper
