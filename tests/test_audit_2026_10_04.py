"""2026-10-04 audit of the Kalshi paper maker (paper only, live_armed stays false).

Each test pins one defect found while auditing mm/unattended/loop.py after the
zero-fill fix: the fill-sampling group waiting for the next 10-minute selection
after a restart, Oct 6 checkpoint counters that reset on every restart, trade
prints stamped with their receive time, and the sampling group ranking markets
by trade count alone."""
import asyncio

import pytest

from mm.unattended import loop as L
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap, trade  # noqa: F401

M2 = "KXGDP-26OCT30-T2"
M3 = "KXPPI-26OCT30-T1"


def _activity(ts, counts, vol=None):
    return {"kind": "activity", "ts": ts, "trades_24h": dict(counts),
            "volume_24h": dict(vol or {m: 900.0 for m in counts})}


def _book(lp, market, ts, yes, no):
    lp.on_frame(snap(market, ts, yes, no))


YES = [(44, 30), (40, 2000)]
NO = [(53, 30), (50, 2000)]


# ------------------------------------------------- 1. sampling group startup
def test_sampling_group_forms_when_activity_arrives_after_the_first_selection(monkeypatch):
    """Restart: the first selection runs before the REST trade counts exist, so
    the group was empty until the next selection 600 s later."""
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    _book(lp, M, T0, YES, NO)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert lp.selection_count == 1 and not lp.sample_markets
    lp.on_frame(_activity(T0 + 20, {M: 40}))
    lp.on_frame({"type": "clock", "ts": T0 + 21})
    assert lp.selection_count == 1, "no second full selection is needed"
    assert M in lp.sample_markets and M in lp.resting
    assert (lp.resting[M]["yes_cents"], lp.resting[M]["no_cents"]) == (44, 53)


def test_sample_budget_is_reserved_before_any_group_exists(monkeypatch):
    """The ranked pass must leave room for a group that forms later."""
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    _book(lp, M, T0, YES, NO)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert not lp.sample_markets
    assert lp.sample_reserve_usd == pytest.approx(80.0)
    assert lp.venue_budget["kalshi"] == pytest.approx(lp.venue_budgets()["kalshi"] - 80.0)


def test_top_up_adds_members_without_requoting_existing_ones(monkeypatch):
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    for m in (M, M2):
        lp.on_frame(program(m, rank_penalty_per_day=1e6))
        _book(lp, m, T0, YES, NO)
    lp.on_frame(_activity(T0, {M: 40}))
    lp.on_frame({"type": "clock", "ts": T0 + 10})
    assert lp.sample_markets == {M}
    quotes_before = lp.quotes_total
    order = lp.sim.orders[f"{M}:yes"]
    lp.on_frame(_activity(T0 + 30, {M: 40, M2: 25}))
    lp.on_frame({"type": "clock", "ts": T0 + 31})
    assert lp.sample_markets == {M, M2} and M2 in lp.resting
    assert lp.quotes_total == quotes_before + 1          # only the new member was quoted
    assert lp.sim.orders[f"{M}:yes"] is order            # queue position untouched


def test_top_up_respects_group_size_per_event_and_budget(monkeypatch):
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    monkeypatch.setenv("LIP_SAMPLE_N", "2")
    lp = newloop(bankroll=1500.0)
    sibling = "KXCPI-26OCT30-T4"
    for m in (M, sibling, M2, M3):
        lp.on_frame(program(m, event="KXCPI-26OCT30" if m in (M, sibling) else None,
                            rank_penalty_per_day=1e6))
        _book(lp, m, T0, YES, NO)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(_activity(T0 + 5, {M: 90, sibling: 80, M2: 70, M3: 60}))
    lp.on_frame({"type": "clock", "ts": T0 + 6})
    assert len(lp.sample_markets) == 2
    lp.on_frame(_activity(T0 + 60, {M: 90, sibling: 80, M2: 70, M3: 60}))
    lp.on_frame({"type": "clock", "ts": T0 + 61})
    assert len(lp.sample_markets) == 2                  # full: no further additions


def test_top_up_is_held_while_the_skew_guard_is_active(monkeypatch):
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    _book(lp, M, T0, YES, NO)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp._skew_active = True
    lp.on_frame(_activity(T0 + 20, {M: 40}))
    lp.on_frame({"type": "clock", "ts": T0 + 21})
    assert not lp.sample_markets and M not in lp.resting


def test_top_up_is_a_noop_when_sampling_is_off(monkeypatch):
    monkeypatch.delenv("LIP_SAMPLE_ENABLE", raising=False)
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    _book(lp, M, T0, YES, NO)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(_activity(T0 + 20, {M: 40}))
    lp.on_frame({"type": "clock", "ts": T0 + 21})
    assert not lp.sample_markets and lp.sample_reserve_usd == 0.0


def test_session_seeds_trade_counts_before_the_first_subscription(monkeypatch):
    """The first subscription already includes the most traded books, so the
    sampling group does not wait for the background refresh behind the
    market-metadata fetch."""
    for k, v in (("LIP_WS_SUB_MAX", "2"), ("LIP_WS_SUB_CORE", "1"), ("LIP_WS_SUB_ROTATE", "0"),
                 ("LIP_WS_SUB_ACTIVE", "1"), ("LIP_ACTIVITY_PROBE_N", "5")):
        monkeypatch.setenv(k, v)
    events = []

    class Reader:
        def __init__(self, **kw):
            pass

        def get(self, path, params=None):
            events.append(("get", path, (params or {}).get("ticker")))
            n = {"A": 1, "B": 5, "C": 50}.get((params or {}).get("ticker"), 0)
            return {"trades": [{}] * n}

    class Sock:
        def __init__(self, **kw):
            self._ws = self

        async def connect(self):
            events.append(("connect",))

        async def subscribe(self, channels, tickers=None):
            events.append(("subscribe", sorted(tickers or [])))

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    import mm.venues.readonly as RO
    monkeypatch.setattr(RO, "ReadOnlyKalshiTransport", Reader)
    monkeypatch.setattr(RO, "ReadOnlyMarketSocket", Sock)

    async def idle(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(L, "_background", idle)
    ctx = {"cache": None, "meta": None, "fed": {"A": 1, "B": 1, "C": 1}, "refresh_s": 600.0,
           "programs": [{"market": "A"}], "lock": None, "ranked": ["A", "B", "C"],
           "volume": {"A": 10.0, "B": 20.0, "C": 30.0}}
    frames = []
    asyncio.run(L._readonly_books_session({"key_id": "k", "ws_url": "wss://x"}, object(), object(),
                                          frames.append, {"frames": False}, ctx=ctx))
    subs = [e for e in events if e[0] == "subscribe" and e[1] != []]
    assert any(f.get("kind") == "activity" for f in frames)
    assert subs and "C" in subs[0][1] and "A" in subs[0][1]   # core A + most traded C, first batch


# ------------------------------------------------- 2. checkpoint survives restarts
def _delta(ts, lag, market=M, price="0.3800", qty="0.00", side="yes"):
    return {"type": "orderbook_delta", "ts": ts, "exchange_ts": ts - lag,
            "msg": {"market_ticker": market, "price_dollars": price, "delta_fp": qty, "side": side}}


def _campaign(monkeypatch, path):
    """A loop with one real print fill from the sampling group, one clock-skew
    trip that pulled a quote, and a fill whose 5-minute markout is measured."""
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.attach_state(str(path))
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    lp.on_frame(_activity(T0, {M: 40}))
    lp.on_frame(snap(M, T0, YES, NO))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(snap(M, T0 + 2, [(40, 2000)], NO))        # the queue ahead of us is gone
    lp.on_frame(trade(M, T0 + 3, "t1", 44, 50, "no"))
    assert lp.fills_by_source == {"kalshi:print": 1} and lp.sample_fills_n == 1
    lp.on_frame({"type": "clock", "ts": T0 + 400})           # 300 s markout is due
    assert lp.fill_marks[-1]["markout_300s"] is not None
    for k in range(3):
        lp.on_frame(_delta(T0 + 401 + 0.1 * k, 6.0))
    assert lp.pulls.get("clock_skew") == 1
    return lp


def test_checkpoint_counters_survive_a_restart(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    lp = _campaign(monkeypatch, path)
    assert lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(path))
    assert lp2.kill is None
    lp2.on_frame({"type": "clock", "ts": T0 + 1000})
    cp = lp2.checkpoint_report()
    assert cp["kalshi_fills"]["real_print"] == 1 and cp["kalshi_fills"]["from_sampling_group"] == 1
    assert cp["clock_skew_pulls"]["session"] == 1 and cp["clock_skew_pulls"]["last_24h"] == 1
    assert cp["markout_5m"]["fills"] == 1
    assert cp["clock_skew_pulls"]["session_hours"] == pytest.approx(1000 / 3600, abs=0.01)


def test_restored_pending_markouts_complete_after_the_restart(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.attach_state(str(path))
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    lp.on_frame(_activity(T0, {M: 40}))
    lp.on_frame(snap(M, T0, YES, NO))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(snap(M, T0 + 2, [(40, 2000)], NO))
    lp.on_frame(trade(M, T0 + 3, "t1", 44, 50, "no"))
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(path))
    lp2.on_frame(program(M, rank_penalty_per_day=1e6))
    lp2.on_frame(snap(M, T0 + 100, [(46, 50)], [(52, 50)]))
    lp2.on_frame({"type": "clock", "ts": T0 + 400})
    assert lp2.fill_marks[0]["markout_300s"] == pytest.approx(10 * (47.0 - 44) / 100.0)


def test_unreadable_checkpoint_fields_never_latch_a_kill(monkeypatch, tmp_path):
    import json
    path = tmp_path / "state.json"
    lp = _campaign(monkeypatch, path)
    lp.save_state(force=True)
    data = json.loads(path.read_text())
    data.update({"fills_by_source": "oops", "pulls": [1], "skew_pull_hours": {"x": "y"},
                 "fill_marks": [{"market": 3}, "junk"], "sample_fills_n": "n/a"})
    path.write_text(json.dumps(data))
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(path))
    assert lp2.kill is None and lp2.state_error is None
    assert lp2.fills_by_source == {} and lp2.fill_marks == []


def test_state_files_from_before_the_fix_still_load(monkeypatch, tmp_path):
    import json
    path = tmp_path / "state.json"
    lp = _campaign(monkeypatch, path)
    lp.save_state(force=True)
    data = json.loads(path.read_text())
    for k in ("fills_by_source", "sample_fills_n", "pulls", "skew_pull_hours", "fill_marks",
              "campaign_start_ts", "skew_trips_n", "skew_clears_n"):
        data.pop(k, None)
    path.write_text(json.dumps(data))
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(path))
    assert lp2.kill is None and lp2.fills_total == 1 and lp2.fills_by_source == {}


# ------------------------------------------------- 3. trade time is the exchange's
def _ws_trade(ts_recv, tid, yes_c, count, taker, *, ts_ms=None, ts_s=None, market=M):
    body = {"trade_id": tid, "market_ticker": market, "count_fp": f"{count:.2f}",
            "yes_price_dollars": f"{yes_c/100:.4f}", "no_price_dollars": f"{(100-yes_c)/100:.4f}",
            "taker_side": taker}
    if ts_ms is not None:
        body["ts_ms"] = int(ts_ms)
    if ts_s is not None:
        body["ts"] = int(ts_s)
    return {"type": "trade", "ts": ts_recv, "trade": body}


def _resting_at_touch(monkeypatch):
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    lp.on_frame(_activity(T0, {M: 40}))
    lp.on_frame(snap(M, T0, YES, NO))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    o = lp.sim.orders[f"{M}:yes"]
    # the order as if placed at T0 + 10 with nobody ahead of it
    o.activation_ts, o.queue_ahead = T0 + 10.25, 0.0
    assert o.price_cents == 44
    return lp, o


def test_a_print_that_happened_before_the_order_is_not_a_fill_even_if_it_arrives_late(monkeypatch):
    """The websocket trade message has no created_time: the loop stamped it
    with the RECEIVE time, so a print from before our order existed (lag of
    a second or two) looked like it hit us."""
    lp, o = _resting_at_touch(monkeypatch)
    lp.on_frame(_ws_trade(T0 + 11.0, "late", 44, 5, "no", ts_ms=(T0 + 9.0) * 1000))
    assert lp.fills == []


def test_a_print_after_activation_still_fills_and_ts_seconds_is_understood(monkeypatch):
    lp, _o = _resting_at_touch(monkeypatch)
    lp.on_frame(_ws_trade(T0 + 12.0, "ok", 44, 5, "no", ts_ms=(T0 + 11.5) * 1000))
    assert [f["trade_id"] for f in lp.fills] == ["ok"]
    lp2, _ = _resting_at_touch(monkeypatch)
    lp2.on_frame(_ws_trade(T0 + 12.0, "sec", 44, 5, "no", ts_s=int(T0 + 9)))
    assert lp2.fills == []                                  # whole-second ts, before activation


def test_a_missing_or_future_exchange_time_falls_back_to_receive_time(monkeypatch):
    lp, _o = _resting_at_touch(monkeypatch)
    lp.on_frame(_ws_trade(T0 + 12.0, "none", 44, 5, "no"))
    assert [f["trade_id"] for f in lp.fills] == ["none"]
    lp2, _ = _resting_at_touch(monkeypatch)
    lp2.on_frame(_ws_trade(T0 + 12.0, "fut", 44, 5, "no", ts_ms=(T0 + 500) * 1000))  # bad clock
    assert [f["trade_id"] for f in lp2.fills] == ["fut"]


# ------------------------------------------------- 4. screen rank: unknown/empty books
SCREEN_FRAME = {"market": M, "series": "KXCPI", "target_size": 1000, "period_reward_usd": 500,
                "period_seconds": 86400, "discount_factor": 0.5}
NORMAL_META = {"yes_bid": 0.44, "yes_ask": 0.47, "yes_bid_size": 300, "yes_ask_size": 300,
               "volume_24h": 3000}


def _rank(meta):
    from mm.unattended.screen import rank_score
    return rank_score(SCREEN_FRAME, meta, category="Economics", days=30)


@pytest.mark.parametrize("meta", [
    {},                                                                    # no cached book at all
    {"yes_bid": 0.0, "yes_ask": 1.0, "yes_bid_size": 0, "yes_ask_size": 0, "volume_24h": 0},
    {"yes_bid": 0.44, "yes_ask": 0.47, "volume_24h": 3000},                # sizes unknown
    {"yes_bid": 0.0, "yes_ask": 0.47, "yes_bid_size": 0, "yes_ask_size": 50},   # one-sided
])
def test_screen_never_ranks_an_unknown_or_one_sided_book_above_a_real_market(meta):
    """An empty book scored share 1.0 on a $5 capital floor: ~80x a normal
    market, so such markets took the top-ranked (core) websocket slots."""
    dead, normal = _rank(meta), _rank(NORMAL_META)
    assert dead["score"] < normal["score"]
    assert dead["share"] == 0.0 and dead["book_known"] is False
    assert normal["book_known"] is True and normal["share"] > 0


def test_screen_orders_candidates_with_known_books_first():
    from mm.unattended.screen import MetaCache, screen
    cache = MetaCache()
    cache.series["KXCPI"] = {"category": "Economics", "fee_type": "quadratic_with_maker_fees", "ts": 1e12}
    now = 1_790_000_000.0
    base = {"status": "active", "tick_1c": True, "exchange_index": 1, "effective_close_ts": now + 30 * 86400, "volume_24h": 500.0}
    cache.markets["KXCPI-26NOV30-A"] = dict(base, **NORMAL_META)
    cache.markets["KXCPI-26NOV30-B"] = dict(base)                         # never saw a book
    frames = [dict(SCREEN_FRAME, market=m, program_id=m, start_ts=now - 3600, end_ts=now + 86400 * 7,
                   close_ts=now + 30 * 86400) for m in cache.markets]
    chosen, _stats = screen(frames, cache, now=now, top=10)
    assert [f["market"] for f in chosen][0] == "KXCPI-26NOV30-A"


# ------------------------------------------------- 5. sampling group: fill hazard, not trade count
def test_sampling_group_prefers_the_market_we_can_actually_fill(monkeypatch):
    """100 trades/day behind a 5,000-contract touch is a worse sample than
    40 trades/day behind 20 contracts: queue ahead decides how long a fill
    takes."""
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    monkeypatch.setenv("LIP_SAMPLE_N", "1")
    lp = newloop(bankroll=1500.0)
    for m in (M, M2):
        lp.on_frame(program(m, rank_penalty_per_day=1e6))
    lp.on_frame(_activity(T0, {M: 100, M2: 40}))
    lp.on_frame(snap(M, T0, [(44, 5000), (40, 2000)], [(53, 5000), (50, 2000)]))
    lp.on_frame(snap(M2, T0, [(44, 20), (40, 2000)], [(53, 20), (50, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 10})
    lp._select(T0 + 11)
    assert lp.sample_markets == {M2}


def test_sampling_order_is_unchanged_when_queues_are_equal(monkeypatch):
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    monkeypatch.setenv("LIP_SAMPLE_N", "1")
    lp = newloop(bankroll=1500.0)
    for m in (M, M2):
        lp.on_frame(program(m, rank_penalty_per_day=1e6))
        lp.on_frame(snap(m, T0, YES, NO))
    lp.on_frame(_activity(T0, {M: 100, M2: 40}))
    lp.on_frame({"type": "clock", "ts": T0 + 10})
    lp._select(T0 + 11)
    assert lp.sample_markets == {M}


# ------------------------------------------------- 6. Polymarket US paper (audit)
def _pm_loop(monkeypatch, latency_ms=250):
    from tests.test_patch21 import _pm_book, _pm_prog
    monkeypatch.delenv("LIP_DURABLE_RESERVE", raising=False)
    monkeypatch.delenv("LIP_SIZE_LADDER", raising=False)
    monkeypatch.delenv("LIP_SKEW_ENABLE", raising=False)
    monkeypatch.setenv("LIP_CROSS_GUARD", "1")
    lp = L.RunLoop(mode="paper", bankroll=5000, carry_forward=True, latency_ms=latency_ms)
    slug, m = "rtc-bb-2026-10-01-a", "PMUS:rtc-bb-2026-10-01-a"
    _pm_prog(lp)
    _pm_book(lp, slug, [(0.40, 3000)], [(0.45, 3000)], T0 + 1)
    lp.drain_external()
    lp._select(T0 + 2)
    assert m in lp.resting
    return lp, slug, m


def _crossing_frame(slug, data_ts):
    from mm.unattended import pmus_paper as P
    book = {"bids": [(0.30, 3000)], "offers": [(0.35, 100)], "state": "MARKET_STATE_OPEN",
            "shares_traded": None, "last_px": None}
    frame = P.book_frame(slug, book, T0 + 10)
    frame["data_ts"] = data_ts
    return frame


def test_a_polled_book_older_than_our_order_cannot_fill_it(monkeypatch):
    """The latency check used the loop clock; a cached book whose data time
    predates the order showed an offer through our bid and produced a
    'paper_cross' fill (and rebate) for an order that did not exist yet."""
    lp, slug, m = _pm_loop(monkeypatch)
    lp.ext_queue.put(_crossing_frame(slug, T0 + 0.5))
    lp.drain_external()
    lp._guard_resting(T0 + 10)
    assert lp.fills == [] and lp.pm_rebate_usd == 0.0


def test_a_polled_book_newer_than_our_order_still_cross_fills(monkeypatch):
    lp, slug, m = _pm_loop(monkeypatch)
    lp.ext_queue.put(_crossing_frame(slug, T0 + 9))
    lp.drain_external()
    lp._guard_resting(T0 + 10)
    assert [f["source"] for f in lp.fills] == ["paper_cross"] and lp.fills[0]["synthetic"] is True


def test_pmus_refresh_failure_is_retried_within_a_minute():
    """next_refresh was advanced before refresh() ran: a gateway blip at start
    left the feed with zero candidates for LIP_PMUS_REFRESH_S (900 s)."""
    from mm.unattended import pmus_paper as P
    t = [1000.0]
    feed = P.PMUSFeed(L.RunLoop(mode="paper", bankroll=5000), fetch=lambda p: {},
                      clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    calls = []

    def refresh():
        calls.append(t[0])
        if len(calls) == 1:
            raise OSError("gateway down")
        feed._stop.set()

    feed.refresh = refresh
    feed.poll_due = lambda: False
    feed.poll_settlements = lambda: None
    orig_sleep = feed._sleep
    feed._sleep = lambda s: (orig_sleep(s), t[0] > 1400 and feed._stop.set())
    feed._run()
    assert len(calls) == 2 and calls[1] - calls[0] <= 60


# ------------------------------------------------- 7. Oct 10 go/no-go has a producer
def _gate_env(monkeypatch, days=0, fills=1):
    monkeypatch.setenv("LIP_GO_MIN_DAYS", str(days))
    monkeypatch.setenv("LIP_GO_MIN_FILLS", str(fills))


def _filled_and_marked(monkeypatch, mid_after_cents):
    """One sampled fill of 10 YES @44, its 5-minute markout measured against a
    YES mid of ``mid_after_cents``."""
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    lp.on_frame(_activity(T0, {M: 40}))
    lp.on_frame(snap(M, T0, YES, NO))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(snap(M, T0 + 2, [(40, 2000)], NO))
    lp.on_frame(trade(M, T0 + 3, "t1", 44, 50, "no"))
    assert lp.fills_total == 1
    yes_bid, no_bid = mid_after_cents - 1, 100 - (mid_after_cents + 1)
    lp.on_frame(snap(M, T0 + 100, [(yes_bid, 500)], [(no_bid, 500)]))
    lp.on_frame({"type": "clock", "ts": T0 + 400})
    return lp


def test_series_gate_report_passes_a_series_that_earns_and_has_no_adverse_markout(monkeypatch):
    _gate_env(monkeypatch)
    lp = _filled_and_marked(monkeypatch, mid_after_cents=47)        # +3c after 5 min
    lp.settle(M, "yes")
    rep = lp.series_gate_report(accrual=lp.live_accrual())
    row = rep["series"]["KXCPI"]
    assert row["settled_fills"] == 1 and row["fills"] == 1
    assert row["markout_5m_cost_usd"] < 0                           # favourable = negative cost
    assert row["trading_usd"] == pytest.approx(10 * (1.0 - 0.44))   # payout - cost
    assert row["go"] is True and row["why"] == "go"
    assert "KXCPI" in rep["go_series"]


def test_series_gate_report_charges_adverse_markout_as_a_positive_cost(monkeypatch):
    _gate_env(monkeypatch)
    lp = _filled_and_marked(monkeypatch, mid_after_cents=30)        # -14c after 5 min
    lp.settle(M, "yes")
    row = lp.series_gate_report(accrual=lp.live_accrual())["series"]["KXCPI"]
    assert row["markout_5m_cost_usd"] > 0
    assert row["go"] is False


def test_series_gate_report_requires_days_fills_and_a_measured_markout(monkeypatch):
    _gate_env(monkeypatch, days=5, fills=30)
    lp = _filled_and_marked(monkeypatch, mid_after_cents=47)
    lp.settle(M, "yes")
    row = lp.series_gate_report(accrual=lp.live_accrual())["series"]["KXCPI"]
    assert row["go"] is False and row["why"] == "days"
    _gate_env(monkeypatch)
    lp2 = newloop(bankroll=1500.0)
    lp2.on_frame(program(M))
    lp2.on_frame(snap(M, T0, YES, NO))
    lp2._note_fill({"market_ticker": M, "side": "yes", "count": 10, "price_cents": 44, "ts": T0 + 1}, T0 + 1)
    lp2.settle(M, "yes")
    row2 = lp2.series_gate_report(accrual=lp2.live_accrual())["series"]["KXCPI"]
    assert row2["go"] is False and row2["why"] == "markout_unmeasured"


def test_series_gate_accumulators_survive_a_restart(monkeypatch, tmp_path):
    _gate_env(monkeypatch)
    path = tmp_path / "state.json"
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = _filled_and_marked(monkeypatch, mid_after_cents=47)
    lp.attach_state(str(path))
    lp.settle(M, "yes")
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(path))
    lp2.on_frame({"type": "clock", "ts": T0 + 500})
    row = lp2.series_gate_report()["series"]["KXCPI"]
    assert row["settled_fills"] == 1 and row["markout_5m_fills"] == 1 and row["fills"] == 1


def test_status_carries_the_series_gate(monkeypatch):
    from mm.status_page import status_payload
    _gate_env(monkeypatch)
    lp = _filled_and_marked(monkeypatch, mid_after_cents=47)
    assert "series_gate" in status_payload(lp.live_snapshot())


# ------------------------------------------------- 8. reliability
def test_checkpoint_reports_skew_trips_as_well_as_quote_pulls(monkeypatch, tmp_path):
    lp = _campaign(monkeypatch, tmp_path / "state.json")
    cp = lp.checkpoint_report()
    assert cp["clock_skew_pulls"]["trips"] == 1 and cp["clock_skew_pulls"]["session"] == 1


def test_pm_us_frames_are_applied_without_any_kalshi_frame(monkeypatch, tmp_path):
    """drain_external ran only inside the Kalshi frame callback: a quiet or
    down Kalshi socket froze the Polymarket US books."""
    from mm.unattended.service import EngineTimer
    from tests.test_patch21 import _pm_book, _pm_prog
    lp = L.RunLoop(mode="paper", bankroll=5000, carry_forward=True)
    lp.pmus = object()                     # a PM US feed is attached (its thread only put()s frames)
    _pm_prog(lp)
    _pm_book(lp, "rtc-bb-2026-10-01-a", [(0.40, 3000)], [(0.45, 3000)], T0 + 1)
    assert "PMUS:rtc-bb-2026-10-01-a" not in lp.programs or not lp.accruals["PMUS:rtc-bb-2026-10-01-a"].book.book.yes_bids
    timer = EngineTimer(lp, heartbeat=str(tmp_path / "hb"), kill_path=str(tmp_path / "KILL"))
    timer.tick()
    m = "PMUS:rtc-bb-2026-10-01-a"
    assert m in lp.programs and lp.accruals[m].book.book.yes_bids


# ------------------------------------------------- 9. per-account reward cap in selection
def _km_pool(pool, cap=None):
    from mm.selector import KalshiMarket
    return KalshiMarket(market=M, series="KXCPI", period_reward_usd=pool, period_seconds=7 * 86400,
                        seconds_left=7 * 86400, discount_factor=0.5, target_size=100,
                        yes_bids=[(44, 10)], no_bids=[(53, 10)], max_reward_usd=cap)


def test_reward_per_day_honours_max_reward_per_account():
    """max_reward_per_account was applied to the accrual but not to selection,
    so a capped pool ranked as if the whole pool were available."""
    from mm.selector import kalshi_share, reward_per_day
    share = kalshi_share(_km_pool(500.0), 44, 53, 100)
    assert share > 0.5
    uncapped = reward_per_day(share, _km_pool(500.0))
    capped = reward_per_day(share, _km_pool(500.0, cap=7.0))
    assert capped == pytest.approx(7.0 / 7.0) and uncapped > 10 * capped
    assert reward_per_day(share, _km_pool(500.0, cap=10_000.0)) == pytest.approx(uncapped)


def test_loop_passes_the_account_cap_to_selection():
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, max_reward_usd=5.0))
    lp.on_frame(snap(M, T0, YES, NO))
    assert lp._km(M).max_reward_usd == 5.0


# ------------------------------------------------- 10. measured markout reaches selection
def _with_series_markout(usd, contracts, n):
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, YES, NO))
    lp.series_acc["KXCPI"] = {"fills": n, "fees": 0.0, "settled_fills": 0, "settled_usd": 0.0,
                              "mk5_usd": usd, "mk5_contracts": contracts, "mk5_n": n}
    return lp


def test_measured_5m_markout_raises_the_adverse_selection_charge():
    from mm.selector import EMPIRICAL_MIN_N, adverse_cost_per_contract_day
    base = adverse_cost_per_contract_day(_with_series_markout(0, 0, 0)._km(M))
    lp = _with_series_markout(-5.0, 100.0, 10)                 # -5c per contract over 10 fills
    km = lp._km(M)
    assert km.empirical_markout_cents == pytest.approx(-5.0) and km.empirical_n == 10 >= EMPIRICAL_MIN_N
    assert adverse_cost_per_contract_day(km) > base


def test_a_lucky_sample_never_turns_adverse_selection_into_a_reward():
    from mm.selector import adverse_cost_per_contract_day
    base = adverse_cost_per_contract_day(_with_series_markout(0, 0, 0)._km(M))
    km = _with_series_markout(+8.0, 100.0, 10)._km(M)          # +8c: favourable
    assert km.empirical_markout_cents == 0.0
    assert adverse_cost_per_contract_day(km) > 0
    assert adverse_cost_per_contract_day(km) <= base


def test_markout_contracts_accumulate_and_persist(monkeypatch, tmp_path):
    lp = _campaign(monkeypatch, tmp_path / "state.json")
    acc = lp.series_acc["KXCPI"]
    assert acc["mk5_n"] == 1 and acc["mk5_contracts"] == pytest.approx(10.0)
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "state.json"))
    assert lp2.series_acc["KXCPI"]["mk5_contracts"] == pytest.approx(10.0)


# ------------------------------------------------- 11. optional hard activity floor in the screen
def _screen_two(volume_dead, monkeypatch, floor=None):
    from mm.unattended.screen import MetaCache, screen
    if floor is None:
        monkeypatch.delenv("LIP_ACTIVITY_MIN_VOL", raising=False)
    else:
        monkeypatch.setenv("LIP_ACTIVITY_MIN_VOL", str(floor))
    cache = MetaCache()
    cache.series["KXCPI"] = {"category": "Economics", "fee_type": "quadratic_with_maker_fees", "ts": 1e12}
    now = 1_790_000_000.0
    base = {"status": "active", "tick_1c": True, "exchange_index": 1, "effective_close_ts": now + 30 * 86400}
    cache.markets["KXCPI-26NOV30-A"] = dict(base, **NORMAL_META)
    cache.markets["KXCPI-26NOV30-B"] = dict(base, **dict(NORMAL_META, volume_24h=volume_dead))
    frames = [dict(SCREEN_FRAME, market=m, program_id=m, start_ts=now - 3600, end_ts=now + 86400 * 7,
                   close_ts=now + 30 * 86400) for m in cache.markets]
    return screen(frames, cache, now=now, top=10)


def test_screen_activity_floor_is_off_by_default(monkeypatch):
    chosen, stats = _screen_two(0.0, monkeypatch)
    assert {f["market"] for f in chosen} == {"KXCPI-26NOV30-A", "KXCPI-26NOV30-B"}


def test_screen_activity_floor_drops_dead_markets_and_counts_them(monkeypatch):
    chosen, stats = _screen_two(3.0, monkeypatch, floor=20)
    assert [f["market"] for f in chosen] == ["KXCPI-26NOV30-A"]
    assert stats["reasons"].get("below_min_activity") == 1


def test_screen_activity_floor_keeps_markets_with_unknown_volume(monkeypatch):
    """Unknown is not dead: only a KNOWN volume under the floor excludes."""
    chosen, _stats = _screen_two(None, monkeypatch, floor=20)
    assert len(chosen) == 2


# ------------------------------------------------- review fixes (2026-10-04, Grok Bot)
def test_top_up_never_takes_a_market_the_ranked_pass_is_quoting(monkeypatch):
    """A top-up between selections must not re-quote a ranked-pass market at
    best bid (that replaced its reward quote and double-spent its capital)."""
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M))                                   # ranked pass will quote it
    _book(lp, M, T0, YES, NO)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert lp.selection_count == 1 and M in lp.resting and not lp.sample_markets
    main_quote = dict(lp.resting[M])
    lp.on_frame(_activity(T0 + 20, {M: 400}))                 # now the most traded market
    lp.on_frame({"type": "clock", "ts": T0 + 21})
    assert lp.selection_count == 1
    assert M not in lp.sample_markets
    assert (lp.resting[M]["yes_cents"], lp.resting[M]["no_cents"]) == (main_quote["yes_cents"],
                                                                      main_quote["no_cents"])


def test_top_up_without_new_members_keeps_the_quoted_stats(monkeypatch):
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    _book(lp, M, T0, YES, NO)
    lp.on_frame(_activity(T0, {M: 40}))
    lp.on_frame({"type": "clock", "ts": T0 + 10})
    assert lp.sample_markets == {M} and lp.sample_stats.get("quoted") == 1
    lp.on_frame({"type": "clock", "ts": T0 + 50})             # top-up scan, nobody new
    assert lp.sample_stats.get("quoted") == 1 and "capital_usd" in lp.sample_stats


def test_series_gate_settled_fills_exclude_synthetic_fills(monkeypatch):
    _gate_env(monkeypatch)
    lp = _filled_and_marked(monkeypatch, mid_after_cents=47)
    lp._record_fill({"market_ticker": M, "side": "yes", "price_cents": 44, "count": 5.0,
                     "ts": T0 + 401, "source": "synthetic", "trade_id": "syn-1", "synthetic": True},
                    T0 + 401)
    lp.settle(M, "yes")
    row = lp.series_gate_report(accrual=lp.live_accrual())["series"]["KXCPI"]
    assert row["fills"] == 1
    assert row["settled_fills"] == 1, "a synthetic fill must not count toward the 30 settled fills"


def test_activity_probe_with_nothing_to_probe_does_not_latch_the_period(monkeypatch):
    """Cold metadata cache at startup: no volume_24h yet. The probe must not
    block the background refresh for LIP_ACTIVITY_REFRESH_S."""
    calls = []

    class Reader:
        def get(self, path, params=None):
            calls.append(params["ticker"])
            return {"trades": [{}] * 25}

    frames = []
    ctx = {"fed": {"A": 1}, "volume": {}}
    assert asyncio.run(L._refresh_activity(Reader(), ctx, frames.append, now=T0)) == 0
    assert "activity_at" not in ctx and not calls and not frames
    ctx["volume"] = {"A": 50.0}
    assert asyncio.run(L._refresh_activity(Reader(), ctx, frames.append, now=T0 + 60)) == 1
    assert ctx["activity_at"] == T0 + 60 and calls == ["A"] and frames[-1]["trades_24h"] == {"A": 25}


def test_failed_activity_probe_is_retried_not_latched(monkeypatch):
    monkeypatch.setattr(L.time, "sleep", lambda s: None)

    class Down:
        def get(self, path, params=None):
            raise ConnectionError("gateway blip")

    ctx = {"fed": {"A": 1}, "volume": {"A": 50.0}, "activity_at": None}
    with pytest.raises(ConnectionError):
        asyncio.run(L._refresh_activity(Down(), ctx, lambda f: None, now=T0))
    assert ctx["activity_at"] is None


def test_markout_due_long_ago_is_not_measured_with_a_late_mid(monkeypatch, tmp_path):
    """A fill restored after a long restart gap: its 60 s / 300 s markouts are
    far past due; measuring them with the current mid would feed the gate and
    selection a wrong number."""
    path = tmp_path / "state.json"
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.attach_state(str(path))
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    lp.on_frame(_activity(T0, {M: 40}))
    lp.on_frame(snap(M, T0, YES, NO))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(snap(M, T0 + 2, [(40, 2000)], NO))
    lp.on_frame(trade(M, T0 + 3, "t1", 44, 50, "no"))
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(path))
    lp2.on_frame(program(M, rank_penalty_per_day=1e6))
    lp2.on_frame(snap(M, T0 + 900, [(60, 50)], [(38, 50)]))
    lp2.on_frame({"type": "clock", "ts": T0 + 901})           # 300 s due at T0+303, grace 150 s
    mark = lp2.fill_marks[0]
    assert mark["markout_60s"] is None and mark["markout_300s"] is None
    assert lp2.series_acc["KXCPI"]["mk5_n"] == 0
    lp2.on_frame({"type": "clock", "ts": T0 + 1805})          # 1800 s horizon is on time
    assert lp2.fill_marks[0]["markout_1800s"] is not None


def test_top_up_waits_for_the_reselect_after_a_disconnect(monkeypatch):
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    _book(lp, M, T0, YES, NO)
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert not lp.sample_markets
    lp.trades_24h = {M: 40}
    lp.connected = True
    lp._sample_dirty = True
    lp._reselect_pending = True
    lp._top_up_sample(T0 + 30)
    assert not lp.sample_markets
    lp._reselect_pending = False                              # control: it would pick M
    lp._top_up_sample(T0 + 60)
    assert M in lp.sample_markets
