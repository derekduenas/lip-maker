"""Review fixes, risk limits: inventory in caps, daily loss, resting-size
clamps, PM US market cap, capital pin, optimize cut, unusable books, clock
skew, fills/min alert."""
import logging
from decimal import Decimal

import pytest

from mm.unattended import loop as L
from tests.test_review_loop_pnl import (
    M, T0, apply_policy, newloop, program, snap, trade, _filled_loop,
)

EV = "KXCPI-26OCT30"
MS = [f"{EV}-T{i}" for i in range(6)]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    apply_policy(monkeypatch)
    for name in ("LIP_SINGLE_FILL_CAP_USD", "LIP_WD_MAX_CAPITAL_USD"):
        monkeypatch.delenv(name, raising=False)
    import monitor.alerts
    monkeypatch.setattr(monitor.alerts, "alert", lambda *a, **k: None)


def _event_loop(n=6, warmup=5):
    lp = L.RunLoop(mode="paper", carry_forward=True, first_select_warmup_s=warmup, bankroll=5000.0)
    for m in MS[:n]:
        lp.on_frame(program(m, event=EV))
    for m in MS[:n]:
        lp.on_frame(snap(m, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 6})
    return lp


# ------------------------------------------------------------------ 7. inventory in caps
def test_filled_inventory_counts_in_risk_caps_and_budgets():
    lp = _filled_loop()
    cost = sum(f["count"] * f["price_cents"] / 100.0 for f in lp.fills)
    lp._cancel(M, "test")  # resting commitment released
    assert float(lp.risk.market_usd[M]) == pytest.approx(cost)
    assert float(lp.risk.venue_usd["kalshi"]) == pytest.approx(cost)
    assert lp.locked_usd()["kalshi"] == pytest.approx(cost)
    flat = lp.venue_budgets(locked={})
    assert lp.venue_budgets()["kalshi"] == pytest.approx(flat["kalshi"] - cost)
    # a quote that would push the market past per_market (inventory + new) is a cap skip
    per_market = float(lp.risk.limits.per_market_usd)
    d = lp.risk.check_quote(market=M, venue="kalshi",
                            add_usd=Decimal(str(per_market - cost + 1.0)))
    assert not d.allowed and d.reason.startswith("per_market")


def test_paired_inventory_is_locked_capital():
    lp = newloop()
    lp.on_frame(program(M))
    lp.position[M] = {"yes": 100.0, "no": 100.0, "yes_cost": 45.0, "no_cost": 50.0, "fees": 0.0,
                      "venue": "kalshi"}
    lp._sync_inventory(M)
    assert float(lp.inv_committed[M]) == pytest.approx(95.0)
    assert lp._unpaired_usd(M) == 0.0


# ------------------------------------------------------------------ 8. daily loss / reconnect / ramp
def test_daily_mtm_loss_reaches_check_quote_and_kills():
    lp = _filled_loop()
    limit = float(lp.risk.limits.daily_loss_usd)
    # a big losing position marked at the current book
    lp.position[M]["yes"] += 10_000.0
    lp.position[M]["yes_cost"] += 10_000.0 * 0.40
    lp.on_frame(snap(M, T0 + 3, [(10, 500)], [(85, 500)]))
    assert float(lp.daily_pnl_usd()) < -limit
    assert lp._quote(M, 10, 85, 10, T0 + 4) is False
    assert lp.kill is not None and lp.kill["reason"].startswith("daily_loss")


def test_reconnect_after_a_long_gap_latches_the_risk_kill(monkeypatch):
    lp = _filled_loop()
    lp.on_frame({"kind": "disconnect", "ts": T0 + 5, "reason": "x"})
    lp.on_frame({"kind": "reconnect", "ts": T0 + 2000, "stale_s": 1995.0})
    assert lp.kill is not None and lp.kill["reason"].startswith("disconnect")
    assert lp.risk.killed
    lp2 = _filled_loop()
    lp2.on_frame({"kind": "disconnect", "ts": T0 + 5, "reason": "x"})
    lp2.on_frame({"kind": "reconnect", "ts": T0 + 15, "stale_s": 10.0})
    assert lp2.kill is None and not lp2.resting and not lp2.risk.killed


def test_limits_use_the_configured_ramp(monkeypatch):
    import config.settings
    from config import constitution
    monkeypatch.setattr(config.settings, "RAMP_PHASE", 2)
    lp = newloop(bankroll=5000.0)
    assert float(lp.risk.limits.daily_loss_usd) == pytest.approx(
        min(5000 * 0.05, constitution.MAX_DAILY_LOSS_BY_RAMP[2]))


# ------------------------------------------------------------------ 9. clamps before fills
def test_default_single_fill_cap_is_at_most_the_market_inventory_cap(monkeypatch):
    lp = newloop()
    assert lp.fill_cap <= 25.0
    monkeypatch.setenv("LIP_SINGLE_FILL_CAP_USD", "60")
    assert newloop().fill_cap == 60.0


def test_resting_size_is_clamped_to_inventory_room():
    lp = _event_loop()
    assert lp.resting
    for m, q in lp.resting.items():
        for sd in ("yes", "no"):
            if q[sd] > 0:
                assert q[sd] * q[f"{sd}_cents"] / 100.0 <= 25.0 + 1e-9


def test_fills_recheck_siblings_and_never_breach_the_event_cap():
    lp = _event_loop()
    assert len(lp.resting) >= 2
    for i, m in enumerate(MS):
        lp.on_frame(trade(m, T0 + 7 + i * 0.1, f"t{i}", 39, 5000, "no"))
        held = sum(lp._unpaired_usd(x) for x in MS)
        assert held <= 75.0 + 1e-9
        # every sibling's resting YES could fill without breaching the event cap
        room_e = 75.0 - held
        for x, q in lp.resting.items():
            if q.get("yes", 0) > 0 and lp._unpaired(x, "yes") >= 0:
                assert q["yes"] * q["yes_cents"] / 100.0 <= room_e + 1e-6


# ------------------------------------------------------------------ 10. PM US market cap
def test_pmus_market_cap_limits_sizing(monkeypatch):
    monkeypatch.setenv("LIP_PMUS_MARKET_CAP_USD", "30")
    monkeypatch.setenv("LIP_MARKET_INV_CAP_USD", "1000")
    monkeypatch.setenv("LIP_EVENT_INV_CAP_USD", "1000")
    lp = newloop()
    pm = program("PMUS:abc-def-2026-12-01", series="PMUS:abc-def", venue="pmus")
    lp.on_frame(pm)
    lp.on_frame(snap("PMUS:abc-def-2026-12-01", T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    km = next(k for k in lp._markets() if k.market == "PMUS:abc-def-2026-12-01")
    curve = lp._size_curve(km, [10, 20, 30, 50, 100], ("yes", "no"), 0.0)
    assert curve and all(row[2] <= 30.0 + 1e-9 for row in curve)
    monkeypatch.delenv("LIP_PMUS_MARKET_CAP_USD")
    assert max(row[2] for row in lp._size_curve(km, [10, 20, 30, 50, 100], ("yes", "no"), 0.0)) > 30.0


# ------------------------------------------------------------------ 11. capital vs watchdog
def test_budget_over_watchdog_cap_warns_and_clamps(monkeypatch, caplog):
    monkeypatch.setenv("LIP_WD_MAX_CAPITAL_USD", "1600")
    lp = newloop(bankroll=5000.0)
    lp.on_frame(program("PMUS:abc-def-2026-12-01", series="PMUS:abc-def", venue="pmus"))
    with caplog.at_level(logging.ERROR, logger="lip.risk"):
        msg = lp.check_watchdog_capital()
    assert msg and "LIP_WD_MAX_CAPITAL_USD" in caplog.text
    assert sum(lp.venue_budgets().values()) <= 1600.0 + 1e-6
    assert newloop(bankroll=1500.0).check_watchdog_capital() is None


# ------------------------------------------------------------------ 12. optimize cut
def test_optimize_cut_does_not_drop_series_members_by_raw_objective(monkeypatch):
    monkeypatch.setenv("LIP_EVENT_CAP_FRAC", "0")
    lp = L.RunLoop(mode="paper", carry_forward=True, first_select_warmup_s=5, bankroll=5000.0)
    ms = [f"KXCPI-26OCT{10 + i}-T{i}" for i in range(14)]
    for i, m in enumerate(ms):
        p = program(m, event=m)
        p["period_reward_usd"] = 900.0 if i < 10 else 500.0
        p["rank_penalty_per_day"] = 60.0 if i < 10 else 0.0
        lp.on_frame(p)
    for m in ms:
        lp.on_frame(snap(m, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp._select(T0 + 6)
    # the unpenalised 500-pool markets are the only positive-rank ones and must be quoted
    for m in ms[10:]:
        assert "objective" in lp.last_plan.get(m, {}), m
        assert m in lp.resting, m
    for m in ms[:10]:
        assert m not in lp.resting


# ------------------------------------------------------------------ 13. unusable book
def test_unusable_book_pulls_the_resting_quote():
    lp = _filled_loop()
    lp.on_frame(snap(M, T0 + 3, [(38, 2000)], [(55, 2000)]))
    lp._select(T0 + 3)
    assert M in lp.resting
    lp.accruals[M].book.note_disconnect()
    lp.on_frame({"type": "clock", "ts": T0 + 5})
    assert M not in lp.resting
    assert lp.pulls.get("book_unusable") == 1


# ------------------------------------------------------------------ 14. clock skew
def test_exchange_timestamp_is_read_from_kalshi_frames():
    assert L._exchange_ts({"type": "orderbook_snapshot", "sending_ts_ms": 1669149841234}) == pytest.approx(1669149841.234)
    assert L._exchange_ts({"type": "orderbook_delta", "msg": {"ts_ms": 1669149841000}}) == pytest.approx(1669149841.0)
    assert L._exchange_ts({"type": "orderbook_delta", "msg": {}}) is None


def test_skewed_frame_is_applied_and_pulls_the_quote():
    lp = _filled_loop()
    lp.on_frame(snap(M, T0 + 3, [(38, 2000)], [(55, 2000)]))
    lp._select(T0 + 3)
    assert M in lp.resting
    frame = snap(M, T0 + 4, [(37, 2000)], [(56, 2000)])
    frame["exchange_ts"] = T0 + 4 - 30.0  # 30 s old data
    lp.on_frame(frame)
    assert M not in lp.resting and lp.skew_n == 1
    assert lp.accruals[M].book.book.is_usable()  # frame applied, book still in sequence
    assert max(l.price_cents for l in lp.accruals[M].book.book.yes_bids) == 37


# ------------------------------------------------------------------ 15. fills/min alert
def test_fills_per_minute_kill_alerts_and_is_in_status(monkeypatch):
    import monitor.alerts
    sent = []
    monkeypatch.setattr(monitor.alerts, "alert", lambda *a, **k: sent.append(a))
    lp = _filled_loop()
    for i in range(40):
        if lp.kill is not None:
            break
        lp._record_fill({"market_ticker": M, "side": "yes", "price_cents": 40, "count": 1.0,
                         "trade_id": f"x{i}"}, T0 + 3)
    assert lp.kill is not None and lp.kill["reason"].startswith("fills_per_minute")
    assert sent and sent[0][0] == "CRITICAL" and "fills_per_minute" in sent[0][2]
    st = lp.live_snapshot()
    assert st["engine_alerts"] and "fills_per_minute" in st["engine_alerts"][-1]["message"]
