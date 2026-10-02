"""Patch 21: Polymarket US on the shared engine (PAPER ONLY) + audit fixes."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine.lip_scorer import BookLevel, BookState, OurQuotes, ProgramParams, score_snapshot, snapshot_share
from mm.selector import KalshiMarket, pmus_side_rung, quote_economics, maker_fee_usd
from mm.unattended import loop as L
from mm.unattended import pmus_paper as P

T0 = 1_790_800_000.0  # 2026-09-30 ~16:53 UTC


def _pp(target=1000, df=0.3, ms=None):
    return ProgramParams(market_ticker="PMUS:x", target_size=target, discount_factor=df,
                         period_reward_usd=100, period_seconds=86400, rules="pmus", max_spread_usd=ms)


def _bk(yes, no):
    return BookState(market_ticker="PMUS:x", yes_bids=[BookLevel(p, s) for p, s in yes],
                     no_bids=[BookLevel(p, s) for p, s in no])


# ----------------------------------------------------------------- order block
def test_order_and_write_requests_are_refused():
    for method, path in (("POST", "/v1/orders"), ("POST", "/v1/incentives"),
                         ("DELETE", "/v1/order/1"), ("GET", "/v1/orders/open"),
                         ("GET", "/v1/order/abc/modify"), ("GET", "/v1/portfolio/positions"),
                         ("GET", "/v1/incentives/earnings"), ("PUT", "/v1/markets/x/book")):
        with pytest.raises(P.PMUSOrderBlocked):
            P.check_request(method, path)
    P.check_request("GET", "/v1/incentives?page_size=100&statuses=active")
    P.check_request("GET", "/v1/markets/rtc-x-2026-10-01-abc/book")
    P.check_request("GET", "/v1/market/slug/rtc-x-2026-10-01-abc")


def test_feed_get_goes_through_allowlist():
    seen = []

    def fetch(path):
        P.check_request("GET", path)
        seen.append(path)
        return {}
    f = P.PMUSFeed(L.RunLoop(mode="paper", bankroll=5000), fetch=fetch, sleep=lambda s: None)
    f._get("/v1/markets/a-b/book")
    with pytest.raises(P.PMUSOrderBlocked):
        f._get("/v1/orders")
    assert seen == ["/v1/markets/a-b/book"] and f.stats["blocked_writes"] == 1


# ------------------------------------------------------------ PM US scoring
def test_pmus_docs_example_df_030():
    # docs.polymarket.us/incentives/liquidity: DF 0.30, 1000 at best/1/2/3 ticks
    # -> best gets 1000/1417 = 70.6%, 3 ticks away 27/1417 = 1.9% of that side.
    yes = [(50, 1000), (49, 1000), (48, 1000), (47, 1000)]
    ours = OurQuotes(yes_bids=[BookLevel(50, 1000)], no_bids=[])
    r = score_snapshot(_bk(yes, [(40, 5000)]), ours, _pp(target=4000))
    assert abs(r.our_yes_normalized - 1000 / 1417) < 1e-9
    ours3 = OurQuotes(yes_bids=[BookLevel(47, 1000)], no_bids=[])
    r3 = score_snapshot(_bk(yes, [(40, 5000)]), ours3, _pp(target=4000))
    assert abs(r3.our_yes_normalized - 27 / 1417) < 1e-9
    # one side only, no max spread: that side still pays (half of the second)
    assert abs(snapshot_share(r) - (1000 / 1417) / 2) < 1e-9


def test_pmus_reference_is_best_not_target_fifth():
    # Kalshi would give 50 and 49 full credit (reference = level reaching 200);
    # PM US discounts 49 by DF from the best price.
    yes, no = [(50, 100), (49, 900)], [(45, 1000)]
    ours = OurQuotes(yes_bids=[BookLevel(49, 900)], no_bids=[])
    pm = score_snapshot(_bk(yes, no), ours, _pp(target=1000, df=0.5))
    assert abs(pm.our_yes_normalized - 450 / 550) < 1e-9
    k = score_snapshot(_bk(yes, no), ours, ProgramParams("K", 1000, 0.5, 100))
    assert abs(k.our_yes_normalized - 0.9) < 1e-9


def test_pmus_max_spread_docs_examples():
    # target 1000, maxSpread 0.035: bids reach 1000 by 49c, asks by 51c -> pays
    ours = OurQuotes(yes_bids=[BookLevel(49, 1000)], no_bids=[])
    ok = score_snapshot(_bk([(49, 1000)], [(100 - 51, 1000)]), ours, _pp(df=0.5, ms=0.035))
    assert ok.snapshot_valid and ok.our_yes_normalized == 1.0
    # asks only reach 1000 by 57c -> nobody paid
    bad = score_snapshot(_bk([(49, 1000)], [(100 - 57, 1000)]), ours, _pp(df=0.5, ms=0.035))
    assert not bad.snapshot_valid and snapshot_share(bad) == 0
    # exactly on the limit (47 / 54, 7c gap) pays
    edge = score_snapshot(_bk([(47, 1000)], [(100 - 54, 1000)]),
                          OurQuotes(yes_bids=[BookLevel(47, 1000)]), _pp(df=0.5, ms=0.035))
    assert edge.snapshot_valid
    # one side short of target with a max spread: nobody paid, incl. the full side
    short = score_snapshot(_bk([(49, 1000)], [(49, 500)]), ours, _pp(df=0.5, ms=0.035))
    assert not short.snapshot_valid


def test_pmus_rung_improves_one_tick_and_never_locks(monkeypatch):
    monkeypatch.delenv("LIP_PMUS_MAX_IMPROVE_TICKS", raising=False)
    # thick best bid at 40, offers at 45 (no_bids 55): improving to 41 makes
    # everyone else DF^1 -> best share per $.
    assert pmus_side_rung([(40, 3000)], [(55, 3000)], 100, 1000, 0.5) == 41
    # spread one tick: improving would lock (41 + 59 = 100) -> join 40
    assert pmus_side_rung([(40, 3000)], [(59, 3000)], 100, 1000, 0.5) == 40
    monkeypatch.setenv("LIP_PMUS_MAX_IMPROVE_TICKS", "0")
    assert pmus_side_rung([(40, 3000)], [(55, 3000)], 100, 1000, 0.5) == 40
    assert pmus_side_rung([], [(55, 3000)], 100, 1000, 0.5) is None


def test_pmus_economics_rebate_and_daily_floor():
    m = KalshiMarket(market="PMUS:a-b", series="PMUS:a-b", period_reward_usd=1000, period_seconds=86400,
                     seconds_left=86400, discount_factor=0.5, target_size=1000,
                     yes_bids=[(40, 3000)], no_bids=[(55, 3000)], days_to_settle=20, venue="pmus")
    net, cap, share, yc, nc = quote_economics(m, 100)
    assert (yc, nc) == (41, 56) and share > 0 and abs(cap - 97.0) < 1e-9
    assert maker_fee_usd(m, 50) == pytest.approx(-0.0125 * 0.25)
    m2 = KalshiMarket(market="PMUS:a-c", series="PMUS:a-c", period_reward_usd=5, period_seconds=10 * 86400,
                      seconds_left=10 * 86400, discount_factor=0.5, target_size=1000,
                      yes_bids=[(40, 3000)], no_bids=[(55, 3000)], days_to_settle=20, venue="pmus")
    from mm.selector import reward_per_day
    assert reward_per_day(0.9, m2) == 0.0  # $4.50 period but $0.45/day < $1 (conservative)


def test_kalshi_maker_fee_type_enters_economics():
    base = dict(market="KXA-1", series="KXA", period_reward_usd=100, period_seconds=86400,
                seconds_left=86400, discount_factor=0.5, target_size=1000,
                yes_bids=[(40, 3000)], no_bids=[(55, 3000)], days_to_settle=20)
    free = quote_economics(KalshiMarket(**base), 100)[0]
    paid = quote_economics(KalshiMarket(**base, fee_type="quadratic_with_maker_fees"), 100)[0]
    weird = quote_economics(KalshiMarket(**base, fee_type="new_type"), 100)[0]
    assert paid < free and weird == paid


# -------------------------------------------------------- programs / windows
def test_daily_event_window_is_et_day_and_conservative():
    now = datetime(2026, 10, 1, 18, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    tp = {"period": "daily_event", "start": "2026-10-01T21:30:00Z"}
    ws, we, pool_s = P.program_window(tp, None, None, now)
    d0 = datetime(2026, 10, 1, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert ws == P._ts("2026-10-01T21:30:00Z") and we == d0 + 86400 and pool_s == 86400
    assert P.program_window({"period": "day_of", "start": "2026-10-01T17:00:00Z"},
                            P._ts("2026-10-01T23:00:00Z"), None, now) is not None
    assert P.program_window({"period": "early", "start": "2026-09-20T00:00:00Z"},
                            P._ts("2026-10-01T23:00:00Z"), None, now) is None  # inside 6 h
    assert P._ts("2026-10-01T22:58:00.823226088Z") is not None


REC = [
    {"marketSlug": "rtc-bb-2026-10-01-a", "instrumentState": "INSTRUMENT_STATE_OPEN", "category": "CUL",
     "eventStartTime": "2026-10-01T04:00:00.000Z",
     "timePeriods": [{"programId": "p1", "programType": "liquidityProgram", "status": "active",
                      "start": "2026-10-01T21:30:00Z", "rewardPool": 1000, "discountFactor": 0.25,
                      "targetSize": 200, "period": "daily_event", "maxSpread": 0.055}]},
    {"marketSlug": "rtc-bb-2026-10-01-b", "instrumentState": "INSTRUMENT_STATE_OPEN", "category": "CUL",
     "timePeriods": [{"programId": "p1", "programType": "liquidityProgram", "status": "active",
                      "start": "2026-10-01T21:30:00Z", "rewardPool": 1000, "discountFactor": 0.25,
                      "targetSize": 200, "period": "daily_event"}]},
    {"marketSlug": "aec-nfl-x-y-2026-10-02", "instrumentState": "INSTRUMENT_STATE_OPEN", "category": "SPR",
     "eventStartTime": "2026-10-02T00:15:00Z",
     "timePeriods": [{"programId": "nfl", "programType": "liquidityProgram", "status": "active",
                      "start": "2026-10-01T18:15:00Z", "rewardPool": 140, "discountFactor": 0.5,
                      "targetSize": 1000, "period": "day_of"}]},
    {"marketSlug": "cs2-z-2026-10-01-map1", "instrumentState": "INSTRUMENT_STATE_OPEN",
     "timePeriods": [{"programId": "cs", "programType": "liquidityProgram", "status": "active",
                      "start": "2026-10-01T15:00:00Z", "rewardPool": 4, "discountFactor": 0.5,
                      "targetSize": 1100, "period": "live"}]},
]
NOW = datetime(2026, 10, 1, 22, 0, tzinfo=ZoneInfo("UTC")).timestamp()


def _meta(close_days, tick=0.01, mtype="futures"):
    return {"close_ts": NOW + close_days * 86400, "tick": tick, "market_type": mtype, "category": "culture",
            "active": True, "closed": False, "occurrence_ts": None, "best_bid": 0.3, "best_ask": 0.56,
            "fetched": NOW}


def test_records_policy_pool_split_and_meta(monkeypatch):
    monkeypatch.delenv("LIP_PMUS_POOL_SPLIT", raising=False)
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")  # live policy.conf
    frames, stats, need = P.records_to_programs(REC, {}, now=NOW)
    assert frames == [] and sorted(need) == ["rtc-bb-2026-10-01-a", "rtc-bb-2026-10-01-b"]
    assert stats["reasons"]["sports_match"] == 2  # SPR day_of + uncategorised live
    meta = {"rtc-bb-2026-10-01-a": _meta(15), "rtc-bb-2026-10-01-b": _meta(1.5)}
    frames, stats, need = P.records_to_programs(REC, meta, now=NOW)
    assert [f["market"] for f in frames] == ["PMUS:rtc-bb-2026-10-01-a"]
    f = frames[0]
    # default: the p1 pool (1000) is divided across its 2 member markets
    assert f["venue"] == "pmus" and f["period_reward_usd"] == 500 and f["period_seconds"] == 86400
    assert f["max_spread_usd"] == 0.055 and f["occurrence_ts"] is None and f["exchange_index"] == 0
    assert any(k.startswith("closes_within") for k in stats["reasons"])  # 1.5 d < 48 h policy
    monkeypatch.setenv("LIP_PMUS_POOL_SPLIT", "market")
    frames, _s, _n = P.records_to_programs(REC, {"rtc-bb-2026-10-01-a": _meta(15)}, now=NOW)
    assert frames[0]["period_reward_usd"] == 1000  # whole pool per market, opt-in only
    frames, stats, _n = P.records_to_programs(REC[:1], {"rtc-bb-2026-10-01-a": _meta(15, tick=0.001)}, now=NOW)
    assert frames == [] and stats["reasons"].get("subcent_tick") == 1


def test_book_frame_and_trade_prints():
    book = P.parse_book({"marketData": {"bids": [{"px": {"value": "0.4000"}, "qty": "300"}],
                                        "offers": [{"px": {"value": "0.4500"}, "qty": "200"}],
                                        "stats": {"sharesTraded": "1050", "lastTradePx": {"value": "0.4000"}}}})
    fr = P.book_frame("a-b", book, 5.0)
    assert fr["msg"]["yes_dollars_fp"] == [["0.4000", "300.0000"]]
    assert fr["msg"]["no_dollars_fp"] == [["0.5500", "200.0000"]]
    # the print is capped by the depth drop on the side it hits (bids 400 -> 300 here)
    prev = {"shares_traded": 1000.0, "bb": 0.40, "bo": 0.45, "bids": [(0.40, 400.0)], "offers": [(0.45, 200.0)]}
    tr = P.synth_trades("a-b", prev, book, 5.0)
    assert len(tr) == 1 and tr[0]["trade"]["taker_side"] == "no" and tr[0]["trade"]["count"] == 50
    prev = {"shares_traded": 1000.0, "bb": 0.38, "bo": 0.39, "bids": [(0.38, 10.0)], "offers": [(0.39, 100.0)]}
    assert P.synth_trades("a-b", prev, book, 5.0)[0]["trade"]["taker_side"] == "yes"
    # no depth in the previous poll state -> no print
    assert P.synth_trades("a-b", {"shares_traded": 1000.0, "bb": 0.40, "bo": 0.45}, book, 5.0) == []
    assert P.synth_trades("a-b", None, book, 5.0) == []


# ------------------------------------------------------------ RunLoop parity
def _pm_prog(loop, slug="rtc-bb-2026-10-01-a", pool=1000.0, target=1000, ms=None):
    loop.ext_queue.put({"kind": "program", "venue": "pmus", "market": "PMUS:" + slug,
                        "series": "PMUS:rtc-bb", "program_id": "p1@1", "period_reward_usd": pool,
                        "period_seconds": 86400, "discount_factor": 0.5, "target_size": target,
                        "start_ts": T0 - 3600, "end_ts": T0 + 86400, "close_ts": T0 + 20 * 86400,
                        "days_to_settle": 20, "exchange_index": 0, "days_from_close": True,
                        "rank_score": 0.1, "max_spread_usd": ms, "event_ticker": "PMUS:rtc-bb"})


def _pm_book(loop, slug, bids, offers, ts, traded=None):
    book = {"bids": bids, "offers": offers, "state": "MARKET_STATE_OPEN", "shares_traded": traded,
            "last_px": None}
    loop.ext_queue.put(P.book_frame(slug, book, ts))


def test_runloop_quotes_pm_market_at_rung_with_venue_caps(monkeypatch):
    monkeypatch.delenv("LIP_DURABLE_RESERVE", raising=False)
    monkeypatch.delenv("LIP_SIZE_LADDER", raising=False)
    monkeypatch.setenv("LIP_PMUS_BUDGET_USD", "300")
    loop = L.RunLoop(mode="paper", bankroll=5000, carry_forward=True)
    _pm_prog(loop)
    _pm_book(loop, "rtc-bb-2026-10-01-a", [(0.40, 3000)], [(0.45, 3000)], T0 + 1)
    loop.drain_external()
    m = "PMUS:rtc-bb-2026-10-01-a"
    assert loop.programs[m].venue == "pmus" and loop.accruals[m].params.rules == "pmus"
    loop._select(T0 + 2)
    q = loop.resting[m]
    assert (q["yes_cents"], q["no_cents"]) == (41, 56)
    b = loop.venue_budgets()
    assert b["kalshi"] == pytest.approx(1425.0) and b["pmus"] == pytest.approx(300.0)
    assert float(loop.risk.venue_usd.get("pmus", 0)) > 0 and float(loop.risk.venue_usd.get("kalshi", 0)) == 0
    loop._cancel(m, "t")
    assert float(loop.risk.venue_usd.get("pmus", 0)) == 0
    snap = loop.live_snapshot()
    assert "venues" in snap and snap["venues"]["pmus"]["programs_fed"] == 1


def test_pm_trade_print_fills_and_stale_book_pulls(monkeypatch):
    monkeypatch.delenv("LIP_DURABLE_RESERVE", raising=False)
    monkeypatch.delenv("LIP_SIZE_LADDER", raising=False)
    monkeypatch.delenv("LIP_SKEW_ENABLE", raising=False)
    loop = L.RunLoop(mode="paper", bankroll=5000, carry_forward=True, latency_ms=0)
    slug, m = "rtc-bb-2026-10-01-a", "PMUS:rtc-bb-2026-10-01-a"
    _pm_prog(loop)
    _pm_book(loop, slug, [(0.40, 3000)], [(0.45, 3000)], T0 + 1)
    loop.drain_external()
    loop._select(T0 + 2)
    assert m in loop.resting
    # a seller prints 100 at 0.41 (our improved bid; we are alone at 41 -> filled)
    loop.ext_queue.put({"type": "trade", "ts": T0 + 3, "trade": {
        "trade_id": "pmus:x:1", "ticker": m, "count": 100.0, "yes_price_dollars": "0.4100",
        "no_price_dollars": "0.5900", "taker_side": "no", "created_time": P._iso(T0 + 3)}})
    loop.drain_external()
    assert len(loop.fills) == 1 and loop.fills[0]["side"] == "yes"
    assert loop.pm_rebate_usd == pytest.approx(0.31, abs=0.011)  # 0.0125*100*.41*.59 = 0.30 -> banker's
    assert loop.fill_marks[-1]["venue"] == "pmus"
    # no book for > LIP_PMUS_STALE_S -> pulled
    monkeypatch.setenv("LIP_PMUS_STALE_S", "30")
    loop._guard_resting(T0 + 100)
    assert m not in loop.resting and loop.pulls.get("pmus_stale_book") == 1


def test_paper_cross_fill_instead_of_silent_pull(monkeypatch):
    monkeypatch.setenv("LIP_CROSS_GUARD", "1")
    monkeypatch.delenv("LIP_SKEW_ENABLE", raising=False)
    loop = L.RunLoop(mode="paper", bankroll=5000, latency_ms=0)
    loop.add_program({"market": "KXA-26DEC-T1", "series": "KXA", "period_reward_usd": 100,
                      "period_seconds": 86400, "start_ts": T0 - 3600, "end_ts": T0 + 86400,
                      "close_ts": T0 + 30 * 86400, "target_size": 1000, "days_from_close": True,
                      "exchange_index": 0})
    loop.on_frame({"type": "orderbook_snapshot", "sid": 1, "ts": T0 + 1, "msg": {
        "market_ticker": "KXA-26DEC-T1", "yes_dollars_fp": [["0.40", "3000"]], "no_dollars_fp": [["0.55", "3000"]]}})
    assert loop._quote("KXA-26DEC-T1", 40, 55, 100, T0 + 2)
    # NO bids jump to 62 -> 40 + 62 >= 100: a real order would have hit our YES bid
    loop.on_frame({"type": "orderbook_snapshot", "sid": 1, "ts": T0 + 3, "msg": {
        "market_ticker": "KXA-26DEC-T1", "yes_dollars_fp": [["0.30", "3000"]], "no_dollars_fp": [["0.62", "60"]]}})
    loop._guard_resting(T0 + 3)
    assert [f["side"] for f in loop.fills] == ["yes"] and loop.fills[0]["count"] == 60
    assert "KXA-26DEC-T1" not in loop.resting


def test_demo_mode_never_quotes_pmus():
    class Poster:
        def place(self, **kw):
            raise AssertionError("must not send")
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _pm_prog(loop)
    _pm_book(loop, "rtc-bb-2026-10-01-a", [(0.40, 3000)], [(0.45, 3000)], T0 + 1)
    loop.drain_external()
    loop.mode = "demo"
    loop.poster = Poster()
    assert loop._quote("PMUS:rtc-bb-2026-10-01-a", 40, 55, 100, T0 + 2) is False


def test_refeed_on_program_change_keeps_book():
    fed, sigs, got = {}, {}, []
    f1 = {"kind": "program", "market": "KXA-1", "program_id": "a", "start_ts": 1, "end_ts": 2}
    assert L._feed_programs([f1], fed, got.append, sigs) == ["KXA-1"]
    assert L._feed_programs([dict(f1)], fed, got.append, sigs) == [] and len(got) == 1
    L._feed_programs([dict(f1, program_id="b", start_ts=2, end_ts=3)], fed, got.append, sigs)
    assert len(got) == 2
    loop = L.RunLoop(mode="paper", bankroll=5000)
    row = {"market": "KXA-26DEC-T1", "program_id": "a", "period_reward_usd": 100, "period_seconds": 86400,
           "start_ts": T0 - 3600, "end_ts": T0 + 86400, "target_size": 1000, "exchange_index": 0}
    loop.add_program(row)
    loop.on_frame({"type": "orderbook_snapshot", "sid": 1, "ts": T0 + 1, "msg": {
        "market_ticker": "KXA-26DEC-T1", "yes_dollars_fp": [["0.40", "3000"]], "no_dollars_fp": [["0.55", "3000"]]}})
    acc = loop.accruals["KXA-26DEC-T1"]
    loop.add_program(dict(row, category="Economics"))  # same window -> same accrual
    assert loop.accruals["KXA-26DEC-T1"] is acc
    loop.add_program(dict(row, program_id="b", start_ts=T0 + 86400, end_ts=T0 + 2 * 86400))
    new = loop.accruals["KXA-26DEC-T1"]
    assert new is not acc and new.book.book.is_usable() and loop.refeeds_n == 1


def test_watchdog_covers_pm_venue(tmp_path):
    from mm.safety import lip_watchdog as W
    cfg = W.Config.from_env({"LIP_WD_STATE_DIR": str(tmp_path)}) if hasattr(W.Config, "from_env") else None
    if cfg is None:
        cfg = W.Config({"LIP_WD_STATE_DIR": str(tmp_path)})
    st = {"session_elapsed_s": 10, "last_frame_ts": 1000.0, "paper_capital_usd": 100,
          "venues": {"kalshi": {"capital_usd": 50}, "pmus": {"capital_usd": 450, "resting_n": 2}},
          "pmus": {"book_age_s": 500, "blocked_writes": 1}}
    reasons, info = W.evaluate(cfg, {}, 1000.0, st, None, 1000.0)
    assert any(r.startswith("capital_pmus") for r in reasons)
    assert any(r.startswith("pmus_feed_stale") for r in reasons)
    assert any(r.startswith("pmus_blocked_write") for r in reasons)


def test_trimmed_history_keeps_exact_counts(monkeypatch):
    monkeypatch.setattr(L, "LIST_CAP", 10)
    loop = L.RunLoop(mode="paper", bankroll=5000, carry_forward=True)
    loop.add_program({"market": "KXA-26DEC-T1", "series": "KXA", "period_reward_usd": 100,
                      "period_seconds": 86400, "start_ts": T0 - 3600, "end_ts": T0 + 86400,
                      "close_ts": T0 + 30 * 86400, "target_size": 1000, "exchange_index": 0})
    loop.on_frame({"type": "orderbook_snapshot", "sid": 1, "ts": T0 + 1, "msg": {
        "market_ticker": "KXA-26DEC-T1", "yes_dollars_fp": [["0.40", "3000"]], "no_dollars_fp": [["0.55", "3000"]]}})
    for i in range(25):
        loop._quote("KXA-26DEC-T1", 40, 55, 100, T0 + 2 + i)
    assert loop.quotes_total == 25 and len(loop.quotes) <= 10
    assert loop.live_snapshot()["quotes_n"] == 25
