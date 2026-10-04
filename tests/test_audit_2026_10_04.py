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
