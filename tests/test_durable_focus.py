"""Durable-focus gates: short closes, match series, horizons, suspect plans."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from mm.selector import KalshiMarket, exclusion_reason
from mm.session_gates import plan_is_suspect, plan_per_hundred
from mm.status_page import status_payload
from mm.unattended.loop import (
    RunLoop, apply_market_meta, candidate_tickers, enrich_and_subscribe,
)


def _market(name, *, series=None, days=3.0, category="", exchange_index=2):
    return KalshiMarket(
        market=name, series=series or name.split("-", 1)[0],
        period_reward_usd=50, period_seconds=86400, seconds_left=86400,
        discount_factor=0.5, target_size=100, days_to_settle=days,
        exchange_index=exchange_index, category=category,
    )


def _frame(market, series, *, reward=10.0, days=3.0, category=""):
    return {
        "market": market, "series": series, "period_reward_usd": reward,
        "period_seconds": 86400, "discount_factor": 0.5, "target_size": 100,
        "days_to_settle": days, "category": category,
    }


def test_close_under_24h_is_excluded_and_the_window_is_configurable(monkeypatch):
    soon = _market("KXBRENT-SOON", days=0.5)
    later = _market("KXBRENT-LATER", days=2)
    assert exclusion_reason(soon) == "closes_within_24h"
    assert exclusion_reason(later) == ""
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "0")
    assert exclusion_reason(soon) == ""


def test_sports_and_esports_matches_are_excluded_by_category_and_pattern(monkeypatch):
    assert exclusion_reason(_market("KXTTELITEMATCH-26OCT01A")) == "match_series"
    assert exclusion_reason(_market("KXNBAGAME-26OCT01")) == "match_series"
    assert exclusion_reason(_market("KXFOO-1", series="KXFOO", category="Sports")) == "sports_category"
    assert exclusion_reason(_market("KXFOO-2", series="KXFOO", category="Esports")) == "sports_category"
    assert exclusion_reason(_market("KXBRENT-26OCT07")) == ""
    monkeypatch.setenv("LIP_MATCH_SERIES_DENY", "-")
    monkeypatch.setenv("LIP_SPORTS_CATEGORIES", "-")
    assert exclusion_reason(_market("KXTTELITEMATCH-26OCT01A", category="Sports")) == ""


def test_long_dated_window_is_90_and_120_from_market_close(monkeypatch):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    frame = {
        "market": "KXPRES-1", "series": "KXPRES",
        "end_ts": now + 10 * 86400, "days_to_settle": 10,
    }
    far_close = apply_market_meta(
        frame, {"exchange_index": 2, "close_ts": now + 100 * 86400, "category": ""}, now=now,
    )
    assert far_close["days_to_settle"] == 100
    assert far_close["end_ts"] == frame["end_ts"]
    assert exclusion_reason(_market("KXPRES-1", series="KXPRES", days=far_close["days_to_settle"])).startswith(
        "long_dated_event"
    )
    near_close = apply_market_meta(
        {"market": "KXPRES-2", "series": "KXPRES", "end_ts": now + 200 * 86400},
        {"exchange_index": 2, "close_ts": now + 10 * 86400}, now=now,
    )
    assert near_close["days_to_settle"] == 10
    assert exclusion_reason(_market("KXPRES-2", series="KXPRES", days=10)) == ""
    assert exclusion_reason(_market("KXPRES-30", series="KXPRES", days=30)) == ""
    assert exclusion_reason(_market("KXBRENT-100", days=100)) == ""
    assert exclusion_reason(_market("KXBRENT-130", days=130)).startswith("long_dated_")
    monkeypatch.setenv("LIP_LONG_DATED_EVENT_DAYS", "14")
    assert exclusion_reason(_market("KXPRES-30", series="KXPRES", days=30)).startswith("long_dated_event")


def test_a_rich_plan_is_suspect_on_the_status_payload():
    assert plan_per_hundred(80, 200) == 40
    assert plan_is_suspect(40, 100) is False
    assert plan_is_suspect(41, 100) is True
    quiet = status_payload({
        "paper": True, "mode": "paper", "live_armed": False, "suspect": False,
    })
    assert quiet["suspect"] is False
    assert quiet["suspect_markets"] == []
    flagged = status_payload({
        "paper": True, "mode": "paper", "live_armed": False, "suspect": True,
        "suspect_markets": [{"market": "KXTTELITEMATCH-1", "usd_per_100_day": 80}],
    })
    assert flagged["suspect"] is True
    assert flagged["suspect_markets"][0]["market"] == "KXTTELITEMATCH-1"
    assert flagged["paper"] is True
    assert flagged["live_armed"] is False


def test_selection_waits_until_a_book_arrives_and_skips_a_match():
    loop = RunLoop(mode="paper", bankroll=10_000, select_every=10**9)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    loop.on_frame({
        "kind": "program", "market": "KXBRENT-26OCT07", "series": "KXBRENT",
        "period_reward_usd": 86400, "period_seconds": 86400,
        "discount_factor": 0.5, "target_size": 100, "days_to_settle": 3,
        "exchange_index": 2, "close_ts": now + 3 * 86400,
        "start_ts": now, "end_ts": now + 86400,
    })
    loop.on_frame({"type": "clock", "ts": now + 1})
    assert loop.selection_count == 0
    assert loop.resting == {}
    loop.on_frame({
        "type": "orderbook_snapshot", "sid": 1, "seq": 1, "ts": now + 2,
        "msg": {
            "market_ticker": "KXBRENT-26OCT07",
            "yes_dollars_fp": [["0.5000", "100.00"]],
            "no_dollars_fp": [["0.5000", "100.00"]],
        },
    })
    assert loop.selection_count == 1
    assert "KXBRENT-26OCT07" in loop.resting
    loop.on_frame({
        "kind": "program", "market": "KXTTELITEMATCH-26OCT01A", "series": "KXTTELITEMATCH",
        "period_reward_usd": 86400, "period_seconds": 86400,
        "discount_factor": 0.5, "target_size": 100, "days_to_settle": 3,
        "exchange_index": 2, "category": "Sports", "close_ts": now + 3 * 86400,
        "start_ts": now, "end_ts": now + 86400,
    })
    loop.on_frame({
        "type": "orderbook_snapshot", "sid": 1, "seq": 2, "ts": now + 4,
        "msg": {
            "market_ticker": "KXTTELITEMATCH-26OCT01A",
            "yes_dollars_fp": [["0.5000", "100.00"]],
            "no_dollars_fp": [["0.5000", "100.00"]],
        },
    })
    assert "KXTTELITEMATCH-26OCT01A" not in loop.resting
    assert any(reason == "match_series" for _market, reason in loop.excluded)


def test_subscribe_list_is_the_top_durable_names():
    frames = [
        _frame(f"KXBRENT-{i:04d}", "KXBRENT", reward=float(i + 1))
        for i in range(301)
    ]
    frames.append(_frame("KXTTELITEMATCH-1", "KXTTELITEMATCH", reward=10**9))
    names = candidate_tickers(frames, limit=300)
    assert len(names) == 300
    assert "KXBRENT-0000" not in names
    assert "KXBRENT-0300" in names
    assert "KXTTELITEMATCH-1" not in names


def test_warm_cache_subscribes_before_the_market_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("LIP_MARKET_CACHE", str(tmp_path / "market_meta.json"))
    now = datetime.now(timezone.utc)
    brent = "KXBRENT-26OCT07"
    (tmp_path / "market_meta.json").write_text(json.dumps({
        brent: {
            "ticker": brent, "exchange_index": 2,
            "close_ts": (now + timedelta(days=3)).timestamp(), "category": "",
        },
    }), encoding="utf-8")
    events = []

    class Sock:
        async def subscribe(self, channels, tickers=None):
            events.append(("subscribe", list(tickers or [])))

    class Reader:
        def get(self, path, params=None):
            events.append(("get", path))
            return {"markets": [{
                "ticker": brent, "exchange_index": 2, "category": "",
                "close_time": (now + timedelta(days=3)).isoformat().replace("+00:00", "Z"),
            }]}

    frame = {
        "kind": "program", "market": brent, "series": "KXBRENT",
        "period_reward_usd": 10, "period_seconds": 86400,
        "discount_factor": 0.5, "target_size": 100,
        "start_ts": now.timestamp(), "end_ts": (now + timedelta(days=10)).timestamp(),
    }
    asyncio.run(enrich_and_subscribe(
        Sock(), Reader(), [frame], lambda _row: None, channels=["orderbook_delta"],
    ))
    assert events[0] == ("subscribe", [brent])
    assert events[1][0] == "get"
    assert events[1][1] == "/markets"


def test_readonly_and_demo_sockets_ping_every_20_seconds(tmp_path, monkeypatch):
    import websockets
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from execution.kalshi_ws import WS_PING_INTERVAL_S as demo_interval
    from execution.kalshi_ws import WS_PING_TIMEOUT_S as demo_timeout
    from mm.venues.readonly import ReadOnlyMarketSocket
    from mm.venues.readonly import WS_PING_INTERVAL_S as prod_interval
    from mm.venues.readonly import WS_PING_TIMEOUT_S as prod_timeout

    assert prod_interval == 20 and prod_timeout == 20
    assert demo_interval == 20 and demo_timeout == 20
    seen = {}

    async def fake_connect(url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sock = ReadOnlyMarketSocket(
        api_key="kid", private_key=key,
        url="wss://api.elections.kalshi.com/trade-api/ws/v2",
    )
    asyncio.run(sock.connect())
    assert seen["ping_interval"] == 20
    assert seen["ping_timeout"] == 20
