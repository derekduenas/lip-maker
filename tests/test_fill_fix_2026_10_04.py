"""2026-10-04 Kalshi paper zero-fill fix: clock-skew hysteresis and lag
telemetry, simulator queue keeping and book depletion (with the delta-first
double-count fix), join-the-touch placement, activity weighting, capped and
rotating websocket subscriptions, the fill-sampling group and the Oct 6
checkpoint report."""
import asyncio

import pytest

from execution.paper_fills import PaperFillSimulator
from mm.unattended import gates as G
from mm.unattended import loop as L
from mm.unattended import screen as SC
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap, trade  # noqa: F401

M2 = "KXGDP-26OCT30-T2"


def _delta(ts, lag, market=M, price="0.3800", qty="0.00", side="yes"):
    return {"type": "orderbook_delta", "ts": ts, "exchange_ts": ts - lag,
            "msg": {"market_ticker": market, "price_dollars": price, "delta_fp": qty, "side": side}}


def _quoting(**kw):
    lp = newloop(bankroll=1500.0, **kw)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert M in lp.resting
    return lp


# ------------------------------------------------------------ 1. hysteresis
def test_skew_guard_clears_only_after_the_default_15s_clean_hold(monkeypatch):
    monkeypatch.delenv("LIP_CLOCK_SKEW_CLEAR_S", raising=False)
    lp = _quoting()
    for k in range(3):
        lp.on_frame(_delta(T0 + 2 + 0.1 * k, 6.0))
    assert M not in lp.resting and lp.skew_trips_n == 1
    lp.on_frame(_delta(T0 + 3, 0.2))           # clean, hold starts
    lp.on_frame(_delta(T0 + 10, 0.2))
    assert M not in lp.resting and lp._skew_active
    lp.on_frame(_delta(T0 + 11, 6.0))          # one late frame resets the hold
    lp.on_frame(_delta(T0 + 12, 0.2))
    lp.on_frame(_delta(T0 + 26, 0.2))          # 14 s clean: still held
    assert lp._skew_active
    lp.on_frame(_delta(T0 + 27.5, 0.2))        # 15.5 s clean: cleared, re-quoted
    assert not lp._skew_active and M in lp.resting
    assert lp.skew_clears_n == 1 and lp.pulls["clock_skew"] == 1


def test_flapping_lag_no_longer_requotes_every_few_seconds(monkeypatch):
    monkeypatch.delenv("LIP_CLOCK_SKEW_CLEAR_S", raising=False)
    lp = _quoting()
    ts = T0 + 2
    for _ in range(40):  # 3 late frames then 1 clean, ~2.4 s cycle (the live pattern)
        for _k in range(3):
            lp.on_frame(_delta(ts, 6.0))
            ts += 0.6
        lp.on_frame(_delta(ts, 0.5))
        ts += 0.6
    assert lp.skew_trips_n == 1 and lp.pulls["clock_skew"] == 1


def test_lag_report_and_status_feed_fields():
    lp = _quoting()
    for k in range(5):
        lp.on_frame(_delta(T0 + 2 + k, 1.0 + k * 0.5))
    rep = lp.lag_report()
    assert rep["frames"] == 5 and rep["max_s"] == 3.0 and rep["mean_s"] == 2.0
    feed = lp.live_snapshot()["feed"]
    assert feed["kalshi_lag"]["frames"] == 5
    assert {"clock_skew_trips_n", "clock_skew_clears_n", "inactive_n", "subscriptions"} <= set(feed)


# ------------------------------------------------------------ 2. simulator
class _Lvl:
    def __init__(self, p, q):
        self.price_cents, self.size = p, q


class _Book:
    def __init__(self, yes):
        self.yes_bids = [_Lvl(p, q) for p, q in yes]
        self.no_bids = []


def _tr(tid, yes_c, qty, ts, taker="no"):
    from datetime import datetime, timezone
    return {"trade_id": tid, "ticker": M, "count_fp": str(qty), "yes_price_dollars": f"{yes_c/100:.2f}",
            "taker_side": taker, "created_time": datetime.fromtimestamp(ts, timezone.utc).isoformat()}


def _sim(queue=100):
    sim = PaperFillSimulator(latency_ms=0)
    sim.track(order_id="o", market_ticker=M, side="yes", price_cents=40, size=50,
              book=_Book([(40, queue)]), now=T0)
    return sim


def test_resize_keeps_queue_position():
    sim = _sim()
    sim.orders["o"].queue_ahead = 7
    assert sim.resize("o", 30) and sim.orders["o"].queue_ahead == 7 and sim.orders["o"].remaining == 30
    assert not sim.resize("missing", 1)


def test_depletion_cuts_queue_on_cancellations():
    sim = _sim()
    sim.on_book_level(M, "yes", 40, 20, now=T0 + 1)
    assert sim.orders["o"].queue_ahead == 20
    sim.on_book_level(M, "yes", 40, 500, now=T0 + 2)   # others joined behind us
    assert sim.orders["o"].queue_ahead == 20


def test_delta_before_trade_does_not_double_count_the_print():
    sim = _sim(queue=100)
    sim.on_book_level(M, "yes", 40, 70, now=T0 + 1.0)  # delta of a 30 print arrives first
    fills = sim.apply_trades([_tr("t1", 40, 30, T0 + 1.2)])
    assert fills == [] and sim.orders["o"].queue_ahead == 70


def test_trade_before_delta_and_big_print_fill_correctly():
    sim = _sim(queue=100)
    assert sim.apply_trades([_tr("t1", 40, 30, T0 + 1)]) == []
    sim.on_book_level(M, "yes", 40, 70, now=T0 + 1.1)
    assert sim.orders["o"].queue_ahead == 70
    sim2 = _sim(queue=100)
    sim2.on_book_level(M, "yes", 40, 0, now=T0 + 1.0)  # whole level eaten (delta first)
    fills = sim2.apply_trades([_tr("t2", 40, 150, T0 + 1.1)])
    assert [f["count"] for f in fills] == [50.0]


def test_old_cancellation_does_not_swallow_a_later_print():
    sim = _sim(queue=100)
    sim.on_book_level(M, "yes", 40, 10, now=T0 + 1)     # cancellations
    fills = sim.apply_trades([_tr("t1", 40, 30, T0 + 20)])  # unrelated print 19 s later
    assert [f["count"] for f in fills] == [20.0]


def test_loop_requote_at_same_price_keeps_paper_queue():
    lp = _quoting()
    o = lp.sim.orders[f"{M}:yes"]
    o.queue_ahead = 3.0
    q = lp.resting[M]
    assert lp._quote(M, q["yes_cents"], q["no_cents"], q["yes"], T0 + 2, skewed=True)
    assert lp.sim.orders[f"{M}:yes"] is o and o.queue_ahead == 3.0


def test_loop_depletion_shrinks_queue_from_book_frames():
    lp = _quoting()
    o = lp.sim.orders[f"{M}:yes"]
    price = o.price_cents
    before = o.queue_ahead
    assert before > 0
    lp.on_frame(_delta(T0 + 2, 0.1, price=f"{price/100:.4f}", qty=f"{-(before - 5):.2f}"))
    assert o.queue_ahead == pytest.approx(5.0)


# ------------------------------------------------------------ 3. placement
def test_join_rung_moves_toward_touch_but_never_crosses(monkeypatch):
    from mm.selector import join_rung
    yes = [(44, 10), (42, 500), (40, 2000)]
    monkeypatch.delenv("LIP_JOIN_TOUCH", raising=False)
    assert join_rung(40, yes, [(50, 10)]) == 40
    monkeypatch.setenv("LIP_JOIN_TOUCH", "1")
    monkeypatch.setenv("LIP_JOIN_MAX_TICKS", "2")
    assert join_rung(40, yes, [(50, 10)]) == 42      # capped at ref + 2
    monkeypatch.setenv("LIP_JOIN_MAX_TICKS", "9")
    assert join_rung(40, yes, [(50, 10)]) == 44      # the touch, not above it
    assert join_rung(40, yes, [(57, 10)]) == 42      # 44 + 57 >= 100: back off to 99 - 57
    assert join_rung(44, yes, [(50, 10)]) == 44      # already at the touch
    assert join_rung(None, yes, []) is None


def test_join_touch_quotes_closer_and_keeps_full_reward(monkeypatch):
    from mm.selector import KalshiMarket, kalshi_share, side_rungs
    km = KalshiMarket(market=M, series="KXCPI", period_reward_usd=500, period_seconds=86400,
                      seconds_left=86400, discount_factor=0.5, target_size=1000,
                      yes_bids=[(45, 100), (44, 100), (40, 5000)], no_bids=[(50, 3000)])
    monkeypatch.delenv("LIP_JOIN_TOUCH", raising=False)
    y0, n0 = side_rungs(km, 100)
    monkeypatch.setenv("LIP_JOIN_TOUCH", "1")
    monkeypatch.setenv("LIP_JOIN_MAX_TICKS", "5")
    y1, n1 = side_rungs(km, 100)
    assert y1 > y0 and y1 == 45 and y1 + 50 < 100 and n1 == n0
    assert kalshi_share(km, y1, n1, 100) >= kalshi_share(km, y0, n0, 100) - 1e-9


# ------------------------------------------------------------ 4. activity
def test_activity_weight_and_adjust(monkeypatch):
    monkeypatch.delenv("LIP_ACTIVITY_WEIGHT", raising=False)
    assert SC.activity_weight(0) == 1.0
    monkeypatch.setenv("LIP_ACTIVITY_WEIGHT", "1")
    assert SC.activity_weight(0) == pytest.approx(0.3)
    assert SC.activity_weight(None) == pytest.approx(0.3)
    assert SC.activity_weight(100) == pytest.approx(0.65)
    assert SC.activity_weight(10_000) == 1.0
    assert SC.activity_adjust(2.0, 0.5) == 1.0
    assert SC.activity_adjust(-2.0, 0.5) == -3.0


def test_selection_prefers_the_market_that_trades(monkeypatch):
    monkeypatch.setenv("LIP_ACTIVITY_WEIGHT", "1")
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, volume_24h=0.0))
    lp.on_frame(program(M2, volume_24h=5000.0))
    for m in (M, M2):
        lp.on_frame(snap(m, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp._select(T0 + 2)  # both books known
    plan = lp.last_plan
    assert plan[M2]["rank"] > plan[M]["rank"]
    assert plan[M]["activity_weight"] == pytest.approx(0.3)


# ------------------------------------------------------------ 5. subscriptions
LIM = {"max": 6, "core": 2, "active": 1, "rotate": 2, "rotate_s": 120.0}


def test_plan_subscriptions_pins_core_and_rotates():
    fed = [f"M{i}" for i in range(10)]
    ranked = list(fed)
    got, cur, st = L.plan_subscriptions(fed, ranked, {"M9", "X"}, ["M5"], 0, LIM)
    assert got == {"M9", "M5", "M0", "M1", "M2", "M3"} and st["pinned"] == 1
    got2, cur2, _ = L.plan_subscriptions(fed, ranked, {"M9"}, ["M5"], cur, LIM)
    assert {"M0", "M1", "M5", "M9"} <= got2 and got2 != got   # rotating slice moved
    many = {f"M{i}" for i in range(10)}
    got3, _, _ = L.plan_subscriptions(fed, ranked, many, [], 0, LIM)
    assert got3 == many                                        # pins kept above the cap
    allm, _, st4 = L.plan_subscriptions(fed, ranked, set(), [], 0, dict(LIM, max=0))
    assert allm == set(fed) and st4["mode"] == "all"


def test_default_cap_is_150(monkeypatch):
    for k in ("LIP_WS_SUB_MAX", "LIP_WS_SUB_CORE", "LIP_WS_SUB_ROTATE", "LIP_WS_SUB_ACTIVE"):
        monkeypatch.delenv(k, raising=False)
    lim = L.sub_limits()
    assert (lim["max"], lim["core"], lim["rotate"], lim["active"]) == (150, 60, 40, 20)


def test_apply_subscriptions_marks_unsubscribed_books_inactive(monkeypatch):
    for k, v in (("LIP_WS_SUB_MAX", "3"), ("LIP_WS_SUB_CORE", "1"), ("LIP_WS_SUB_ROTATE", "1"),
                 ("LIP_WS_SUB_ACTIVE", "0")):
        monkeypatch.setenv(k, v)
    calls = []

    class Sock:
        async def subscribe(self, ch, tickers):
            calls.append(("sub", list(tickers)))

        async def unsubscribe_markets(self, names):
            calls.append(("unsub", list(names)))

    frames = []
    ctx = {"fed": {m: 1 for m in ("A", "B", "C", "D")}, "ranked": ["A", "B", "C", "D"],
           "pinned": lambda: {"D"}}
    asyncio.run(L._apply_subscriptions(Sock(), ctx, frames.append))
    assert ctx["subscribed"] == {"D", "A", "B"}
    assert [f["market"] for f in frames if f.get("kind") == "book_inactive"] == ["C"]
    frames.clear(); calls.clear()
    asyncio.run(L._apply_subscriptions(Sock(), ctx, frames.append))   # rotation: B out, C in
    assert ctx["subscribed"] == {"D", "A", "C"}
    assert ("sub", ["C"]) in calls and ("unsub", ["B"]) in calls
    assert [f["market"] for f in frames if f.get("kind") == "book_inactive"] == ["B"]


def test_inactive_book_is_left_out_until_its_next_snapshot():
    lp = _quoting()
    lp.on_frame(program(M2))
    lp.on_frame(snap(M2, T0 + 1, [(40, 2000)], [(55, 2000)]))
    lp.on_frame({"kind": "book_inactive", "market": M2})
    assert M2 in lp.inactive and M2 not in {k.market for k in lp._markets()}
    assert lp._books_ready()
    lp.on_frame(_delta(T0 + 2, 0.1, market=M2))      # stray delta: still inactive
    assert M2 in lp.inactive
    lp.on_frame(snap(M2, T0 + 3, [(40, 2000)], [(55, 2000)]))
    assert M2 not in lp.inactive and M2 in {k.market for k in lp._markets()}


def test_pinned_view_holds_resting_and_positions():
    lp = _quoting()
    lp.on_frame({"type": "clock", "ts": T0 + 3})
    assert M in lp.pinned_view


# ------------------------------------------------------------ 6. fill sampling
def _sampling(monkeypatch, trades=40, **env):
    monkeypatch.setenv("LIP_SAMPLE_ENABLE", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    lp = newloop(bankroll=1500.0)
    # rank would skip M (huge markout penalty): sampling ignores the rank
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    lp.on_frame({"kind": "activity", "ts": T0, "trades_24h": {M: trades}, "volume_24h": {M: 900.0}})
    lp.on_frame(snap(M, T0, [(44, 30), (40, 2000)], [(53, 30), (50, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    return lp


def test_sampling_group_quotes_active_market_at_best_bid(monkeypatch):
    lp = _sampling(monkeypatch)
    q = lp.resting[M]
    assert M in lp.sample_markets
    assert (q["yes_cents"], q["no_cents"]) == (44, 53) and q["yes"] == 10
    assert q["yes_cents"] + 53 < 100 and q["no_cents"] + 44 < 100   # post-only: never crosses
    st = lp.live_snapshot()["fill_sampling"]
    assert st["markets"] == [M] and st["quoted"] == 1 and st["reserve_usd"] > 0


def test_sampling_skips_quiet_markets_and_respects_budget(monkeypatch):
    lp = _sampling(monkeypatch, trades=5)
    assert M not in lp.sample_markets and M not in lp.resting
    lp2 = _sampling(monkeypatch, LIP_SAMPLE_BUDGET_USD=1)
    assert M not in lp2.resting


def test_sampling_off_by_default(monkeypatch):
    monkeypatch.delenv("LIP_SAMPLE_ENABLE", raising=False)
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M, rank_penalty_per_day=1e6))
    lp.on_frame({"kind": "activity", "ts": T0, "trades_24h": {M: 99}})
    lp.on_frame(snap(M, T0, [(44, 30), (40, 2000)], [(53, 30), (50, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert not lp.sample_markets and M not in lp.resting


def test_sample_fill_is_tagged_real_and_counted_for_the_checkpoint(monkeypatch):
    lp = _sampling(monkeypatch)
    lp.on_frame(snap(M, T0 + 2, [(44, 0.0001), (40, 2000)], [(53, 30), (50, 2000)]))  # queue ahead gone
    lp.on_frame(trade(M, T0 + 3, "t1", 44, 50, "no"))
    assert lp.fills and lp.fills[-1]["sample"] is True
    cp = lp.checkpoint_report()
    assert cp["kalshi_fills"]["real_print"] == 1 and cp["kalshi_fills"]["from_sampling_group"] == 1


def test_sample_market_repegs_to_best_bid(monkeypatch):
    lp = _sampling(monkeypatch, LIP_REPEG_MIN_S=1)
    lp.on_frame(snap(M, T0 + 5, [(45, 30), (40, 2000)], [(53, 30), (50, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 7})
    assert lp.resting[M]["yes_cents"] == 45


# ------------------------------------------------------------ 7. checkpoint
def _cp(**kw):
    base = dict(kalshi_fills_print=0, kalshi_fills_cross=0, kalshi_fills_synthetic=0, kalshi_fills_total=0,
                sample_fills=0, skew_pulls_24h=0, skew_pulls_session=0, session_s=7200.0,
                markout_5m_usd=0.0, markout_5m_fills=0, markout_5m_contracts=0.0)
    base.update(kw)
    return G.checkpoint_report(**base)


def test_checkpoint_dates_and_thresholds():
    cfg = G.gate_config()
    assert cfg["checkpoint_date"] == "2026-10-06" and cfg["go_no_go_date"] == "2026-10-10"
    assert cfg["min_real_kalshi_fills"] == 30 and cfg["max_clock_skew_pulls_per_day"] == 100


def test_checkpoint_pass_fail_pending():
    assert _cp()["overall"] == "PENDING"
    ok = _cp(kalshi_fills_print=30, markout_5m_fills=12, markout_5m_contracts=120, markout_5m_usd=-0.6,
             skew_pulls_session=2)
    assert ok["overall"] == "PASS" and ok["markout_5m"]["cents_per_contract"] == -0.5
    bad = _cp(kalshi_fills_print=40, kalshi_fills_synthetic=99, skew_pulls_session=50, session_s=3600.0)
    assert bad["checks"]["clock_skew_pulls_per_day"]["status"] == "FAIL" and bad["overall"] == "FAIL"
    assert _cp(kalshi_fills_print=29, kalshi_fills_cross=50)["checks"]["real_kalshi_fills"]["status"] == "PENDING"
    day = _cp(session_s=90000.0, skew_pulls_24h=99, skew_pulls_session=10_000)
    assert day["checks"]["clock_skew_pulls_per_day"]["value"] == 99.0


def test_status_and_daily_summary_carry_the_checkpoint():
    from mm.status_page import status_payload
    from mm.unattended.health import render_daily_summary
    lp = _quoting()
    snap_ = lp.live_snapshot()
    for key in ("checkpoint", "fills_by_source", "fill_sampling", "clock_skew_pulls_24h"):
        assert key in status_payload(snap_)
    text = render_daily_summary(day="2026-10-04", fills=0, pnl_usd=0, rewards_usd=0,
                                checkpoint=snap_["checkpoint"])
    assert "checkpoint 2026-10-06" in text and "go_no_go 2026-10-10" in text
