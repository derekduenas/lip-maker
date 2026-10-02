"""Review fixes (venue area): PM US paper feed - pool split, period filter,
synthetic prints, book staleness."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mm.unattended import pmus_paper as P
from polymarket.engine import pm_us_lip_scorer as S

NOW = datetime(2026, 10, 1, 22, 0, tzinfo=ZoneInfo("UTC")).timestamp()


def _rec(slug, pid="p1", period="daily_event", pool=900.0, status="active", **extra):
    tp = {"programId": pid, "programType": "liquidityProgram", "status": status,
          "start": "2026-10-01T21:30:00Z", "rewardPool": pool, "discountFactor": 0.5,
          "targetSize": 200, "period": period}
    tp.update(extra)
    return {"marketSlug": slug, "instrumentState": "INSTRUMENT_STATE_OPEN", "category": "CUL",
            "timePeriods": [tp]}


def _meta(close_days=15):
    return {"close_ts": NOW + close_days * 86400, "tick": 0.01, "market_type": "futures",
            "category": "culture", "active": True, "closed": False, "occurrence_ts": None,
            "best_bid": 0.3, "best_ask": 0.56, "fetched": NOW}


# ------------------------------------------------------------------ item 1
def test_pool_split_defaults_to_members(monkeypatch):
    monkeypatch.delenv("LIP_PMUS_POOL_SPLIT", raising=False)
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")
    recs = [_rec(f"rtc-bb-2026-10-01-{c}") for c in "abc"]
    meta = {r["marketSlug"]: _meta() for r in recs}
    frames, stats, _ = P.records_to_programs(recs, meta, now=NOW)
    assert stats["pool_split"] == "members"
    assert frames and all(f["period_reward_usd"] == pytest.approx(300.0) for f in frames)
    monkeypatch.setenv("LIP_PMUS_POOL_SPLIT", "market")
    frames, stats, _ = P.records_to_programs(recs, meta, now=NOW)
    assert stats["pool_split"] == "market"
    assert all(f["period_reward_usd"] == pytest.approx(900.0) for f in frames)
    monkeypatch.setenv("LIP_PMUS_POOL_SPLIT", "bogus")  # unknown -> conservative
    frames, stats, _ = P.records_to_programs(recs, meta, now=NOW)
    assert stats["pool_split"] == "members" and frames[0]["period_reward_usd"] == pytest.approx(300.0)


def test_pmus_paper_and_scorer_use_one_member_count(monkeypatch):
    monkeypatch.delenv("LIP_PMUS_POOL_SPLIT", raising=False)
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")
    # same slug twice + a different period under the same programId
    recs = [_rec("rtc-bb-2026-10-01-a"), _rec("rtc-bb-2026-10-01-a"), _rec("rtc-bb-2026-10-01-b"),
            _rec("rtc-bb-2026-10-01-c", period="early")]
    counts = S.count_pool_members(recs)
    assert counts[S.pool_key("p1", "daily_event")] == 2 and counts[S.pool_key("p1", "early")] == 1
    progs = S.with_shared_pools([p for r in recs[1:] for p in S.parse_incentives(r)])
    by_slug = {p.market_slug: p.n_markets for p in progs}
    assert by_slug == {"rtc-bb-2026-10-01-a": 2, "rtc-bb-2026-10-01-b": 2, "rtc-bb-2026-10-01-c": 1}
    frames, _s, _ = P.records_to_programs(recs[:3], {r["marketSlug"]: _meta() for r in recs}, now=NOW)
    for f in frames:
        assert f["period_reward_usd"] == pytest.approx(S.split_pool_usd(900.0, 2))


# ------------------------------------------------------------------ item 2
def test_pmus_periods_filter_and_unknown_period_rejected(monkeypatch):
    monkeypatch.delenv("LIP_PMUS_PERIODS", raising=False)
    now = NOW
    ws = "2026-10-01T00:00:00Z"
    # unknown period type: rejected even with no filter set
    assert P.program_window({"period": "weekly_mystery", "start": ws}, None, now + 86400, now) is None
    assert P.program_window({"period": "early", "start": ws}, None, now + 86400, now) is not None
    monkeypatch.setenv("LIP_PMUS_PERIODS", "daily_event,pre_day")
    assert P.program_window({"period": "early", "start": ws}, None, now + 86400, now) is None
    assert P.program_window({"period": "pre_day", "start": ws}, None, now + 86400, now) is not None
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")
    recs = [_rec("rtc-bb-2026-10-01-a"), _rec("rtc-bb-2026-10-01-b", pid="p2", period="early")]
    frames, stats, _ = P.records_to_programs(recs, {r["marketSlug"]: _meta() for r in recs}, now=NOW)
    assert [f["market"] for f in frames] == ["PMUS:rtc-bb-2026-10-01-a"]
    assert stats["reasons"].get("period_not_allowed") == 1
    assert stats["periods"] == ["daily_event", "pre_day", "daily"]  # "daily" follows daily_event


def test_unknown_period_record_is_not_fed(monkeypatch):
    monkeypatch.delenv("LIP_PMUS_PERIODS", raising=False)
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")
    recs = [_rec("rtc-bb-2026-10-01-a", period="mystery", end="2026-10-05T00:00:00Z")]
    frames, stats, _ = P.records_to_programs(recs, {"rtc-bb-2026-10-01-a": _meta()}, now=NOW)
    assert frames == [] and stats["reasons"].get("period_not_allowed") == 1


# ------------------------------------------------------------------ item 3
def _book(bids, offers, traded, last):
    return {"bids": bids, "offers": offers, "state": "MARKET_STATE_OPEN",
            "shares_traded": traded, "last_px": last}


def _prev(bids, offers, traded):
    return P.poll_state(_book(bids, offers, traded, None))


def test_synthetic_print_capped_by_depth_reduction():
    # proofs/live/t_pmsynth.py: 1000 bought at the offer, then 1 sold at 0.40.
    # Bid depth at/above 0.40 fell by 1 -> the seller print is at most 1 lot.
    prev = _prev([(0.40, 500.0)], [(0.45, 1500.0)], 10000.0)
    book = _book([(0.40, 499.0)], [(0.45, 500.0)], 11001.0, 0.40)
    trs = P.synth_trades("abc-def-x", prev, book, 3.0)
    assert len(trs) == 1
    t = trs[0]["trade"]
    assert t["taker_side"] == "no" and t["count"] == pytest.approx(1.0) and t["synthetic"] is True


def test_synthetic_print_dropped_without_depth_info_or_reduction():
    book = _book([(0.40, 500.0)], [(0.45, 500.0)], 11001.0, 0.40)
    # legacy prev state (no depth): no print
    assert P.synth_trades("abc-def-x", {"shares_traded": 10000.0, "bb": 0.40, "bo": 0.45}, book, 3.0) == []
    # depth unchanged on the hit side: no print
    prev = _prev([(0.40, 500.0)], [(0.45, 1500.0)], 10000.0)
    assert P.synth_trades("abc-def-x", prev, book, 3.0) == []


def test_synthetic_print_feeds_paper_fill_as_low_fidelity():
    from engine.lip_scorer import BookLevel, BookState
    from execution.paper_fills import PaperFillSimulator
    prev = _prev([(0.40, 500.0)], [(0.45, 1500.0)], 10000.0)
    book = _book([(0.40, 499.0)], [(0.45, 500.0)], 11001.0, 0.40)
    trs = P.synth_trades("abc-def-x", prev, book, 1_000_000.0)
    sim = PaperFillSimulator(latency_ms=0)
    bk = BookState(market_ticker="PMUS:abc-def-x", yes_bids=[BookLevel(40, 500.0)],
                   no_bids=[BookLevel(55, 500.0)])
    sim.track(order_id="o", market_ticker="PMUS:abc-def-x", side="yes", price_cents=41, size=300,
              book=bk, now=1_000_000.0 - 10)
    fills = sim.apply_trades([t["trade"] for t in trs])
    assert [(f["count"], f["synthetic"]) for f in fills] == [(1.0, True)]


# ------------------------------------------------------------------ item 4
class _Resp(dict):
    def __init__(self, data, headers):
        super().__init__(data)
        self.headers = headers


def test_book_frame_uses_server_time_and_records_latency():
    class _Clock:
        t = NOW

        def __call__(self):
            return self.t

    clk = _Clock()

    class _Loop:
        def __init__(self):
            import queue
            self.ext_queue = queue.Queue()
            self.resting_view = frozenset()

    data = {"marketData": {"bids": [{"px": {"value": "0.40"}, "qty": "10"}],
                           "offers": [{"px": {"value": "0.45"}, "qty": "10"}],
                           "state": "MARKET_STATE_OPEN",
                           "transactTime": "2026-10-01T21:00:00Z"}}
    headers = {"Date": "Thu, 01 Oct 2026 22:00:00 GMT", "Age": "15"}

    def fetch(path):
        clk.t += 0.4
        return _Resp(data, headers)
    loop = _Loop()
    f = P.PMUSFeed(loop, fetch=fetch, clock=clk, sleep=lambda s: None)
    f.poll_book("a-b")
    fr = loop.ext_queue.get_nowait()
    assert fr["type"] == "orderbook_snapshot" and fr["ts_source"] == "server"
    # Cloudflare cache HIT: data is ~Age seconds older than the receive time
    assert fr["ts"] == pytest.approx(NOW + 0.4 - 15) and fr["data_ts"] == fr["ts"]
    assert fr["recv_ts"] == pytest.approx(NOW + 0.4)
    assert fr["poll_latency_s"] == pytest.approx(0.4)
    assert fr["server_transact_ts"] == pytest.approx(NOW - 3600)  # recorded, not used
    # no headers -> local time, latency still recorded
    headers.clear()
    f.poll_book("a-b")
    fr = loop.ext_queue.get_nowait()
    assert fr["ts_source"] == "local" and fr["ts"] == pytest.approx(clk.t)
    assert f.stats["last_poll_latency_s"] == pytest.approx(0.4)


def test_server_ts_from_headers():
    # Date kept from origin: min(Date, now - Age) does not double count
    assert P.server_ts_from_headers({"Date": "Thu, 01 Oct 2026 22:00:00 GMT", "Age": "12"},
                                    now=NOW + 12.5) == pytest.approx(NOW)
    # Date rewritten by the CDN to "now": now - Age
    assert P.server_ts_from_headers({"date": "Thu, 01 Oct 2026 22:00:00 GMT", "age": "12"},
                                    now=NOW) == pytest.approx(NOW - 12)
    assert P.server_ts_from_headers({}, now=NOW) is None
    assert P.server_ts_from_headers({"Date": "garbage"}, now=NOW) is None
    assert P.server_ts_from_headers(None, now=NOW) is None


def test_pm_quote_economics_follows_the_shared_split(monkeypatch):
    from mm.selector import PMQuote, pm_quote_economics
    q = PMQuote("m", reward_pool_usd=120, n_markets=12, period_seconds=86400,
                competing_bids=[(0.48, 500)], competing_asks=[(0.52, 500)])
    monkeypatch.delenv("LIP_PMUS_POOL_SPLIT", raising=False)
    shared = pm_quote_economics(q)[3]
    monkeypatch.setenv("LIP_PMUS_POOL_SPLIT", "market")
    whole = pm_quote_economics(q)[3]
    assert whole == pytest.approx(12 * shared) and shared > 0
