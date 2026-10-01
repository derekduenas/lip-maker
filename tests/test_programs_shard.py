"""Program pagination, exchange_index cache, shard frames, real status mode."""
from types import SimpleNamespace

import pytest

from mm.status_page import actual_mode, status_payload
from mm.unattended import loop as L
from mm.venues import readonly as R


class FakeReader:
    def __init__(self, pages=None, markets=None, fail=()):
        self.pages = pages or {}
        self.markets = markets or {}
        self.fail = set(fail)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/incentive_programs":
            return self.pages[(params or {}).get("cursor")]
        ticker = path.rsplit("/", 1)[-1]
        if ticker in self.fail:
            raise R.ReadOnlyHTTPError(f"GET {path} HTTP 500", 500)
        return {"market": {"ticker": ticker, "exchange_index": self.markets[ticker]}}


def test_fetch_all_programs_follows_cursor():
    r = FakeReader(pages={None: {"incentive_programs": [1, 2], "next_cursor": "a"},
                          "a": {"incentive_programs": [3], "next_cursor": "b"},
                          "b": {"incentive_programs": [4], "next_cursor": ""}})
    out = L.fetch_all_programs(r, sleep=lambda s: None)
    assert out["incentive_programs"] == [1, 2, 3, 4]
    assert [c[1].get("cursor") for c in r.calls] == [None, "a", "b"]


def test_fetch_all_programs_stops_on_repeated_cursor():
    r = FakeReader(pages={None: {"incentive_programs": [1], "next_cursor": "a"},
                          "a": {"incentive_programs": [2], "next_cursor": "a"}})
    assert L.fetch_all_programs(r, sleep=lambda s: None)["incentive_programs"] == [1, 2]


def test_cache_looks_up_only_new_and_skips_intraday():
    r = FakeReader(markets={"KXA-1": 0, "KXB-1": 1}, fail={"KXC-1"})
    cache = L.ExchangeIndexCache(sleep=lambda s: None)
    frames = [{"market": "KXA-1", "series": "KXA"}, {"market": "KXB-1", "series": "KXB"},
              {"market": "KXC-1", "series": "KXC"},
              {"market": "KXBTC15M-26OCT01-T1", "series": "KXBTC15M"}]
    cache.enrich(frames, r)
    assert [f.get("exchange_index") for f in frames] == [0, 1, None, None]
    assert cache.lookups == 2 and cache.failures == 1 and cache.skipped == 1
    n = len(r.calls)
    again = [{"market": "KXA-1", "series": "KXA"}, {"market": "KXB-1", "series": "KXB"}]
    cache.enrich(again, r)
    assert len(r.calls) == n and [f["exchange_index"] for f in again] == [0, 1]
    assert all(c[0].startswith("/markets/") or c[0] == "/incentive_programs" for c in r.calls)


def test_feed_programs_once_then_shard_frame_and_runloop_applies_it():
    loop = L.RunLoop(mode="paper")
    fed = {}
    base = {"kind": "program", "market": "KXA-1", "series": "KXA", "period_reward_usd": 10,
            "period_seconds": 86400, "start_ts": 0, "end_ts": 4e9, "target_size": 100}
    assert L._feed_programs([dict(base)], fed, loop.on_frame) == ["KXA-1"]
    assert loop.programs["KXA-1"].exchange_index is None
    assert L._feed_programs([dict(base, exchange_index=2)], fed, loop.on_frame) == []
    assert loop.programs["KXA-1"].exchange_index == 2
    assert L._feed_programs([dict(base, exchange_index=2)], fed, loop.on_frame) == []


def test_actual_mode_reports_real_config():
    assert actual_mode({"LIP_PAPER": "true"}) == {"paper": True, "demo": False,
                                                  "live_armed": False, "mode": "paper"}
    assert actual_mode({"LIP_PAPER": "false", "LIP_DEMO": "true"})["mode"] == "demo"
    armed = actual_mode({"LIP_PAPER": "false", "LIP_LIVE_ACK": "I_ACCEPT_LIVE_RISK"})
    assert armed["live_armed"] is True and armed["paper"] is False
    assert actual_mode({"LIP_PAPER": "false", "LIP_LIVE_ACK": "nope"})["live_armed"] is False


def test_status_payload_not_hardcoded(monkeypatch):
    monkeypatch.setenv("LIP_PAPER", "false")
    monkeypatch.setenv("LIP_LIVE_ACK", "I_ACCEPT_LIVE_RISK")
    out = status_payload({"paper": True})
    assert out["paper"] is False and out["live_armed"] is True
    monkeypatch.setenv("LIP_PAPER", "true")
    monkeypatch.delenv("LIP_LIVE_ACK")
    out = status_payload({})
    assert out["paper"] is True and out["live_armed"] is False and out["mode"] == "paper"


def test_snapshot_reports_exclusion_reasons():
    loop = L.RunLoop(mode="paper")
    loop.excluded = [("a", "shard_unknown"), ("b", "long_dated_91d"), ("c", "long_dated_91d"),
                     ("d", "intraday_15m")]
    snap = loop.live_snapshot()
    assert snap["excluded_reasons"] == {"long_dated": 2, "shard_unknown": 1, "intraday_15m": 1}
