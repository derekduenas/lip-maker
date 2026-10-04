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
