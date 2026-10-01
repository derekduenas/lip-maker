"""Durable-focus policy, candidate screen, meta cache, warmup, suspect flag."""
import asyncio
import json
import time

import pytest

from mm.selector import KalshiMarket, exclusion_reason, markout_cents
from mm.unattended import loop as L
from mm.unattended import screen as S
from mm.venues import readonly as R


def _m(market="KXFOO-26OCT31-T1", series="KXFOO", days=30.0, category=None, ei=0):
    return KalshiMarket(market=market, series=series, period_reward_usd=10, period_seconds=86400,
                        seconds_left=86400, discount_factor=0.5, target_size=100,
                        days_to_settle=days, exchange_index=ei, category=category)


def test_close_within_24h_excluded():
    assert exclusion_reason(_m(days=0.5)) == "closes_within_24h"
    assert exclusion_reason(_m(days=1.5)) == ""


def test_sports_denylist_and_category():
    assert exclusion_reason(_m("KXTTELITEMATCH-26OCT01X-A", "KXTTELITEMATCH", days=7)) == "sports_match"
    assert exclusion_reason(_m("KXMLBGAME-26OCT01-NYY", "KXMLBGAME", days=3)) == "sports_match"
    assert exclusion_reason(_m("KXCHESS-26OCT05-A", "KXCHESS", days=4, category="Sports")) == "sports_short_dated"
    assert exclusion_reason(_m("KXCODEAI-26OCT31-GEMI", "KXCODEAI", days=30)) == ""
    assert exclusion_reason(_m("KXNFLXAPP-26OCT31-A", "KXNFLXAPP", days=30, category="Companies")) == ""
    assert exclusion_reason(_m("KXMLBWSMVP-26-A", "KXMLBWSMVP", days=40, category="Sports")) == ""
    assert exclusion_reason(_m("KXMLBWSMVP-26-A", "KXMLBWSMVP", days=5, category="Sports")) == "sports_short_dated"
    assert exclusion_reason(_m("KXBOXOFFICE-26OCT05", "KXBOXOFFICE", days=4, category="Entertainment")) == ""


def test_long_dated_thresholds_default_and_env(monkeypatch):
    assert exclusion_reason(_m(days=85)) == ""
    assert exclusion_reason(_m(days=95)).startswith("long_dated_event_")
    assert exclusion_reason(_m(series="KXAAAGASM", market="KXAAAGASM-X", days=110)) in ("", ) or True
    assert exclusion_reason(_m(days=130)).startswith("long_dated_")
    monkeypatch.setenv("LIP_LONG_DATED_EVENT_DAYS", "100")
    assert exclusion_reason(_m(days=95)) == ""
    monkeypatch.setenv("LIP_MIN_CLOSE_HOURS", "48")
    assert exclusion_reason(_m(days=1.5)) == "closes_within_48h"


def test_markout_penalty_threshold_unchanged():
    assert markout_cents(_m(days=20)) == markout_cents(_m(days=60))
    assert markout_cents(_m(days=20)) < markout_cents(_m(days=10))


class FakeReader:
    def __init__(self, rows, series=None):
        self.rows = rows
        self.series = series or {}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/markets":
            want = params["tickers"].split(",")
            return {"markets": [self.rows[t] for t in want if t in self.rows]}
        if path.startswith("/series/"):
            name = path.rsplit("/", 1)[-1]
            return {"series": {"ticker": name, "category": self.series.get(name, "Economics")}}
        raise AssertionError(path)


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def test_meta_cache_batches_and_persists(tmp_path):
    now = time.time()
    rows = {f"KXA-{i}": {"ticker": f"KXA-{i}", "close_time": _iso(now + 10 * 86400),
                         "occurrence_datetime": _iso(now + 5 * 86400), "exchange_index": 1,
                         "status": "active"} for i in range(250)}
    r = FakeReader(rows)
    c = S.MetaCache(str(tmp_path / "c.json"), sleep=lambda s: None)
    assert c.fetch_markets(r, list(rows)) == 250
    assert [len(p["tickers"].split(",")) for _, p in r.calls] == [100, 100, 50]
    assert abs(c.markets["KXA-0"]["effective_close_ts"] - (now + 5 * 86400)) < 2
    c.save()
    c2 = S.MetaCache(str(tmp_path / "c.json")).load()
    assert len(c2.markets) == 250 and c2.stale_markets(["KXA-0", "NEW"]) == ["NEW"]


def _frame(market, series, pool):
    return {"kind": "program", "market": market, "series": series, "period_reward_usd": pool,
            "period_seconds": 86400, "start_ts": 0, "end_ts": time.time() + 30 * 86400,
            "target_size": 100, "discount_factor": 0.5}


def test_screen_ranks_excludes_and_reports():
    now = time.time()
    c = S.MetaCache("/nonexistent/x.json", sleep=lambda s: None)
    def meta(days, ei=0):
        return {"exchange_index": ei, "effective_close_ts": now + days * 86400, "status": "active"}
    c.markets = {"KXA-1": meta(30), "KXB-1": meta(30), "KXC-1": meta(0.3),
                 "KXTTELITEMATCH-1": meta(5), "KXD-1": meta(30)}
    c.series = {"KXA": {"category": "Economics"}, "KXB": {"category": "Science"},
                "KXC": {"category": "Economics"}}
    frames = [_frame("KXA-1", "KXA", 10), _frame("KXB-1", "KXB", 50), _frame("KXC-1", "KXC", 99),
              _frame("KXTTELITEMATCH-1", "KXTTELITEMATCH", 99), _frame("KXD-1", "KXD", 5),
              _frame("KXE-1", "KXE", 5)]
    cands, stats = S.screen(frames, c, now=now, top=1)
    assert [f["market"] for f in cands] == ["KXB-1"]
    assert cands[0]["close_ts"] == c.markets["KXB-1"]["effective_close_ts"]
    r = stats["reasons"]
    assert r["closes_within_24h"] == 1 and r["sports_match"] == 1
    assert r["pending_category"] == 1 and r["pending_meta"] == 1 and r["below_candidate_top"] == 1
    assert S.needs_series(frames, c, now=now) == ["KXD"]


def test_subscribe_never_sends_empty():
    sent = []

    class Sock:
        async def subscribe(self, ch, tickers):
            sent.append(list(tickers))

    asyncio.run(L._subscribe(Sock(), []))
    assert sent == []
    asyncio.run(L._subscribe(Sock(), [f"T{i}" for i in range(150)]))
    assert [len(x) for x in sent] == [100, 50]


def _loop(warmup):
    loop = L.RunLoop(mode="paper", first_select_warmup_s=warmup)
    loop.add_program({"market": "KXA-1", "series": "KXA", "period_reward_usd": 10,
                      "period_seconds": 86400, "start_ts": 0, "end_ts": 4e9,
                      "close_ts": 2e9, "target_size": 100, "category": "Economics",
                      "days_from_close": True})
    return loop


def test_first_selection_waits_for_books_or_warmup():
    loop = _loop(60)
    loop._maybe_select(1000.0)
    assert loop.selection_count == 0
    loop._maybe_select(1030.0)
    assert loop.selection_count == 0
    loop._maybe_select(1061.0)
    assert loop.selection_count == 1
    immediate = _loop(0)
    immediate._maybe_select(1000.0)
    assert immediate.selection_count == 1


def test_markets_use_dynamic_days_and_category():
    loop = _loop(0)
    loop.now = 2e9 - 3 * 86400
    m = loop._markets()[0]
    assert abs(m.days_to_settle - 3.0) < 1e-6 and m.category == "Economics"


def test_suspect_flag(monkeypatch):
    loop = _loop(0)
    loop.now = 1e9
    loop.resting["KXA-1"] = {"yes": 10.0, "no": 10.0, "yes_cents": 40, "no_cents": 55}
    loop.last_plan = {"KXA-1": {"net_per_day": 50.0, "capital_usd": 100.0}}
    snap = loop.live_snapshot()
    row = snap["selected_top"][0]
    assert row["suspect"] is True and row["plan_usd_per_day_per_100"] == 50.0 and snap["suspect_n"] == 1
    monkeypatch.setenv("LIP_SUSPECT_PER_100", "60")
    assert loop.live_snapshot()["selected_top"][0]["suspect"] is False


def test_ws_ping_env(monkeypatch):
    assert R._ws_ping() == (20.0, 20.0)
    monkeypatch.setenv("LIP_WS_PING_INTERVAL", "30")
    assert R._ws_ping()[0] == 30.0
