"""Patch 15: size ladder, one-sided/inventory handling, fill cooldown, event window,
re-peg, fast-move and trade-through pulls, fill markouts, idle seconds."""
from datetime import datetime
from zoneinfo import ZoneInfo

from mm.unattended import loop as L
from mm.selector import KalshiMarket, kalshi_share, kalshi_one_sided_share

T0 = 1_790_800_000.0
FAR = T0 + 30 * 86400


def _prog(loop, market, *, pool=100.0, target=1000, series=None, event=None):
    loop.add_program({"market": market, "series": series or market.split("-")[0],
                      "period_reward_usd": pool, "period_seconds": 86400,
                      "start_ts": T0 - 3600, "end_ts": T0 + 86400, "close_ts": FAR,
                      "target_size": target, "days_from_close": True, "rank_score": 0.1,
                      "exchange_index": 0, "event_ticker": event})


def _book(loop, market, yes, no, ts, seq=None):
    loop.on_frame({"type": "orderbook_snapshot", "sid": 1, "seq": seq, "ts": ts,
                   "msg": {"market_ticker": market,
                           "yes_dollars_fp": [[f"{p / 100:.2f}", str(s)] for p, s in yes],
                           "no_dollars_fp": [[f"{p / 100:.2f}", str(s)] for p, s in no],
                           "yes": [[p, s] for p, s in yes], "no": [[p, s] for p, s in no]}})


def test_size_ladder_env(monkeypatch):
    monkeypatch.delenv("LIP_SIZE_LADDER", raising=False)
    assert L.size_ladder(100) == [100.0]
    monkeypatch.setenv("LIP_SIZE_LADDER", "500, 100,bad,1000,100")
    assert L.size_ladder(100) == [100.0, 500.0, 1000.0]


def test_ticker_event_day_and_window(monkeypatch):
    ts = L.ticker_event_day_ts("KXTRUMPMENTIONB-26OCT01-CAFE")
    assert ts == datetime(2026, 10, 1, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert L.ticker_event_day_ts("KXTSNOWFALLBIGSKYM-26DEC-T25") is None
    two = L.ticker_event_day_ts("KXSERBIAELECTIONCALL-26NOV01-26OCT15")
    assert two == datetime(2026, 10, 15, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert L.event_anchor_ts("KXRT-VER-45", 123.0) == 123.0
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXTRUMPMENTIONB-26OCT01-CAFE")
    monkeypatch.setenv("LIP_EVENT_WINDOW_HOURS", "0")
    assert not loop._in_event_window("KXTRUMPMENTIONB-26OCT01-CAFE", ts)
    monkeypatch.setenv("LIP_EVENT_WINDOW_HOURS", "6")
    assert loop._in_event_window("KXTRUMPMENTIONB-26OCT01-CAFE", ts - 5 * 3600)
    assert not loop._in_event_window("KXTRUMPMENTIONB-26OCT01-CAFE", ts - 7 * 3600)
    monkeypatch.setenv("LIP_EVENT_PROVEN_SERIES", "KXTRUMPMENTIONB")
    assert not loop._in_event_window("KXTRUMPMENTIONB-26OCT01-CAFE", ts)


def test_one_sided_share_is_about_half():
    m = KalshiMarket(market="X", series="X", period_reward_usd=100, period_seconds=86400,
                     seconds_left=86400, discount_factor=0.5, target_size=1000,
                     yes_bids=[(10, 3000.0)], no_bids=[(80, 3000.0)], days_to_settle=10)
    two = kalshi_share(m, 10, 80, 300)
    one = kalshi_one_sided_share(m, "yes", 10, 300)
    assert 0 < one < two and abs(one - two / 2) < 1e-9


def test_ladder_puts_capital_where_marginal_yield_is_highest(monkeypatch):
    monkeypatch.setenv("LIP_SIZE_LADDER", "100,300,500,1000")
    monkeypatch.setenv("LIP_HOLDING_MODEL", "carry")
    monkeypatch.delenv("LIP_DURABLE_RESERVE", raising=False)
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXCHEAP-26DEC-T1", pool=300.0)
    _prog(loop, "KXDEAR-26DEC-T1", pool=20.0)
    _book(loop, "KXCHEAP-26DEC-T1", [(5, 4000)], [(5, 4000)], T0 + 1)
    _book(loop, "KXDEAR-26DEC-T1", [(45, 3000)], [(50, 3000)], T0 + 2)
    loop._select(T0 + 3)
    cheap = loop.resting.get("KXCHEAP-26DEC-T1")
    assert cheap is not None and cheap["yes"] > 100
    assert float(sum(loop.committed.values())) <= loop.alloc_budget_usd + 1e-6
    # legacy (no ladder) stays at the single optimizer size
    monkeypatch.delenv("LIP_SIZE_LADDER")
    loop2 = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop2, "KXCHEAP-26DEC-T1", pool=300.0)
    _book(loop2, "KXCHEAP-26DEC-T1", [(5, 4000)], [(5, 4000)], T0 + 1)
    loop2._select(T0 + 3)
    assert loop2.resting["KXCHEAP-26DEC-T1"]["yes"] == 100


def test_fill_cooldown_drops_filled_side_and_keeps_pairing_side(monkeypatch):
    monkeypatch.setenv("LIP_FILL_COOLDOWN_S", "1800")
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-26DEC-T1")
    _book(loop, "KXA-26DEC-T1", [(40, 3000)], [(55, 3000)], T0 + 1)
    assert loop._quote("KXA-26DEC-T1", 40, 55, 100, T0 + 2)
    loop._note_fill({"market_ticker": "KXA-26DEC-T1", "side": "yes", "count": 20, "price_cents": 40},
                    T0 + 5)
    q = loop.resting["KXA-26DEC-T1"]
    assert q["yes"] == 0 and q["no"] == 100
    assert float(loop.committed["KXA-26DEC-T1"]) == 55.0
    assert loop._side_blocked("KXA-26DEC-T1", "yes", T0 + 100) == "fill_cooldown"
    assert loop._side_blocked("KXA-26DEC-T1", "no", T0 + 100) == ""
    assert loop._side_blocked("KXA-26DEC-T1", "yes", T0 + 2000) == ""
    loop._update_markouts(T0 + 70)
    mark = loop.fill_marks[-1]
    assert mark["markout_60s"] is not None and mark["markout_300s"] is None
    s = loop.markout_summary()
    assert s["fills"] == 1 and s["unpaired_usd"] == 8.0


def test_inventory_caps_block_the_side_that_adds(monkeypatch):
    monkeypatch.setenv("LIP_MARKET_INV_CAP_USD", "5")
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-26DEC-T1", event="KXA-26DEC")
    loop._note_fill({"market_ticker": "KXA-26DEC-T1", "side": "no", "count": 20, "price_cents": 50}, T0)
    assert loop._side_blocked("KXA-26DEC-T1", "no", T0 + 1) == "market_inventory"
    assert loop._side_blocked("KXA-26DEC-T1", "yes", T0 + 1) == ""
    monkeypatch.setenv("LIP_EVENT_INV_CAP_USD", "8")
    _prog(loop, "KXA-26DEC-T2", event="KXA-26DEC")
    assert loop._side_blocked("KXA-26DEC-T2", "yes", T0 + 1) == "event_inventory"


def test_repeg_follows_reference_and_fast_move_pulls(monkeypatch):
    monkeypatch.setenv("LIP_REPEG_MIN_S", "1")
    monkeypatch.setenv("LIP_PULL_MOVE_CENTS", "5")
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-26DEC-T1")
    _book(loop, "KXA-26DEC-T1", [(40, 3000)], [(55, 3000)], T0 + 1)
    assert loop._quote("KXA-26DEC-T1", 40, 55, 100, T0 + 2)
    _book(loop, "KXA-26DEC-T1", [(41, 3000)], [(55, 3000)], T0 + 5)
    loop._guard_resting(T0 + 5)
    assert loop.resting["KXA-26DEC-T1"]["yes_cents"] == 41 and loop.repegs_n == 1
    _book(loop, "KXA-26DEC-T1", [(41, 3000)], [(58, 3000)], T0 + 10)  # no bid +3: re-peg only
    loop._guard_resting(T0 + 10)
    assert "KXA-26DEC-T1" in loop.resting and loop.resting["KXA-26DEC-T1"]["no_cents"] == 58
    _book(loop, "KXA-26DEC-T1", [(41, 3000)], [(60, 3000)], T0 + 20)  # +5 vs placement
    loop._guard_resting(T0 + 20)
    assert "KXA-26DEC-T1" not in loop.resting and loop.pulls.get("fast_move") == 1
    assert loop._policy_block("KXA-26DEC-T1", T0 + 30) == "move_cooldown"


def test_cross_guard_drops_a_side_that_would_take(monkeypatch):
    monkeypatch.setenv("LIP_CROSS_GUARD", "1")
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-26DEC-T1")
    _book(loop, "KXA-26DEC-T1", [(40, 3000)], [(55, 3000)], T0 + 1)
    assert loop._quote("KXA-26DEC-T1", 45, 55, 100, T0 + 2)   # 45 + 55 >= 100: yes dropped
    q = loop.resting["KXA-26DEC-T1"]
    assert q["yes"] == 0 and q["no"] == 100


def test_idle_seconds_are_not_unknown_on_live_path():
    loop = L.RunLoop(mode="paper", bankroll=5000, carry_forward=True)
    _prog(loop, "KXA-26DEC-T1")
    loop.on_frame({"type": "clock", "ts": T0})
    for i in range(1, 20):
        _book(loop, "KXA-26DEC-T1", [(40, 3000)], [(55, 3000)], T0 + i)
    loop.on_frame({"type": "clock", "ts": T0 + 25})
    acc = loop.live_accrual(["KXA-26DEC-T1"])["KXA-26DEC-T1"]
    assert acc["unknown"] == 0 and acc["idle"] >= 20


def test_fast_allocate_matches_allocate_membership(monkeypatch):
    from mm.selector import allocate, fast_allocate
    monkeypatch.setenv("LIP_HOLDING_MODEL", "carry")
    ms = []
    for i, (y, n, pool) in enumerate([(5, 5, 300.0), (45, 50, 20.0), (30, 60, 80.0), (2, 97, 5.0)]):
        ms.append(KalshiMarket(market=f"KXM{i}-26DEC-T1", series=f"KXM{i}", period_reward_usd=pool,
                               period_seconds=86400, seconds_left=86400, discount_factor=0.5,
                               target_size=1000, yes_bids=[(y, 3000.0)], no_bids=[(n, 3000.0)],
                               days_to_settle=30, exchange_index=0))
    slow = allocate(ms, bankroll=500000, chunk=100, max_size=100, per_market_usd=475,
                    per_series_usd=1e9, per_category_usd=1e9)
    fast = fast_allocate(ms, per_market_usd=475, chunk=100)
    key = lambda sel: sorted((r.market, round(r.net_per_day, 9), r.capital_usd) for r in sel.taken)
    assert key(slow) == key(fast) and fast.taken
    assert sorted(slow.excluded) == sorted(fast.excluded)
