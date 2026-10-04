"""Integration fixes on the unattended loop: PM US book data time, synthetic
(low-fidelity) PM US fills, rank penalty units, the calibration report field,
per-market positions in /status, replay's fee default."""
from datetime import datetime, timezone

import pytest

from mm.unattended import loop as L
from tests.test_review_loop_pnl import (
    T0, apply_policy, newloop, program, snap, trade,
)

PM = "PMUS:abc-def-2026-12-01"
K = "KXCPI-26OCT30-T3"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    apply_policy(monkeypatch)
    import monitor.alerts
    monkeypatch.setattr(monitor.alerts, "alert", lambda *a, **k: None)
    for name in ("LIP_MARKET_INV_CAP_USD", "LIP_EVENT_INV_CAP_USD", "LIP_SINGLE_FILL_CAP_USD",
                 "LIP_SKEW_ENABLE", "LIP_FILL_COOLDOWN_S", "LIP_CROSS_GUARD", "LIP_WD_MAX_CAPITAL_USD",
                 "LIP_SIZE_LADDER", "LIP_EVENT_CAP_FRAC", "LIP_FV_ENABLE", "LIP_PULL_MOVE_CENTS",
                 "LIP_REPEG_MIN_S", "LIP_EVENT_WINDOW_HOURS", "LIP_PMUS_MARKET_CAP_USD",
                 "LIP_EVENT_CALENDAR_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIP_SELECTION_DUMP", "off")


def pm_program(**extra):
    return program(PM, venue="pmus", series="PMUS:abc-def", fee_type="pmus_maker_rebate",
                   reward=5000.0, **extra)


def pm_snap(ts, yes, no, data_ts=None):
    row = snap(PM, ts, yes, no)
    row["venue"] = "pmus"
    if data_ts is not None:
        row["data_ts"] = data_ts
    return row


def pm_loop():
    lp = newloop()
    lp.on_frame(pm_program())
    lp.on_frame(pm_snap(T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert PM in lp.resting and lp.resting[PM]["yes_cents"] == 41
    return lp


# ------------------------------------------------------------- 3b data time
def test_pmus_book_freshness_uses_the_polled_data_time():
    lp = newloop()
    lp.on_frame(pm_program())
    lp.on_frame({"type": "clock", "ts": T0})
    # the poller's frame is 25 s old by data time; drain_external clamps
    # the frame ts to the loop clock but the book age must not reset
    frame = pm_snap(T0 - 25, [(40, 100)], [(55, 100)], data_ts=T0 - 25)
    lp.ext_queue.put(frame)
    assert lp.drain_external() == 1
    assert lp._book_ts[PM] == pytest.approx(T0 - 25)
    assert lp._book_fresh(PM, T0 + 4)
    assert not lp._book_fresh(PM, T0 + 10)   # 35 s of data age > LIP_PMUS_STALE_S (30)


def test_kalshi_book_time_is_the_frame_time():
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 100)], [(55, 100)]))
    assert lp._book_ts[K] == T0


# ------------------------------------------------------------- 3c synthetic
def _pm_print(ts, tid, yes_c, count, taker):
    row = trade(PM, ts, tid, yes_c, count, taker)
    row["venue"] = "pmus"
    row["synthetic"] = True
    row["trade"]["synthetic"] = True
    return row


def test_synthetic_pmus_fill_is_counted_and_labelled():
    lp = pm_loop()
    lp.on_frame(_pm_print(T0 + 2, "pmus:abc:1", 40, 50, "no"))
    assert lp.fills and lp.fills[-1].get("synthetic") is True
    st = lp.live_snapshot()
    assert st["fills_n"] == 1 and st["fills_synthetic_n"] == 1
    assert st["venues"]["pmus"]["fills_n"] == 1
    assert st["venues"]["pmus"]["synthetic_fills_n"] == 1
    assert "low" in st["venues"]["pmus"]["fill_fidelity"]
    assert st["venues"]["kalshi"]["synthetic_fills_n"] == 0
    bucket = [b for b in st["buckets"].values() if b["fills_n"]][0]
    assert bucket["synthetic_fills_n"] == 1
    assert st["fills_detail"][-1]["synthetic"] is True


def test_pmus_cross_fill_on_a_polled_book_is_synthetic(monkeypatch):
    monkeypatch.setenv("LIP_CROSS_GUARD", "1")
    lp = pm_loop()
    # the next poll shows an offer at 59 (NO bid 41): crosses our YES 41
    lp.on_frame(pm_snap(T0 + 3, [(40, 2000)], [(59, 30), (55, 2000)]))
    cross = [f for f in lp.fills if f.get("source") == "paper_cross"]
    assert cross and all(f.get("synthetic") is True for f in cross)
    assert lp.live_snapshot()["fills_synthetic_n"] == len(cross)


def test_kalshi_fill_is_not_synthetic():
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(trade(K, T0 + 2, "t1", 30, 5000, "no"))
    st = lp.live_snapshot()
    assert st["fills_n"] == 1 and st["fills_synthetic_n"] == 0


# ------------------------------------------------------------- 3d penalty units
def test_rank_penalty_is_scaled_to_the_evaluated_size(monkeypatch):
    """rank_penalty_per_day is $/day per 100 contracts per side; a loop
    evaluating net at chunk=50 must subtract half of it, as _size_curve does."""
    monkeypatch.setenv("LIP_ACTIVITY_WEIGHT", "0")  # isolate the penalty arithmetic
    from mm.selector import quote_economics
    probe = newloop(chunk=50.0)
    probe.on_frame(program(K))
    probe.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    net50, cap50, *_ = quote_economics(probe._markets()[0], 50.0)
    assert net50 > 0
    penalty_100 = 1.5 * net50    # unscaled: net - penalty < 0; scaled: net - 0.75 net > 0
    lp = newloop(chunk=50.0)
    lp.on_frame(program(K, rank_penalty_per_day=penalty_100))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert K not in getattr(lp, "rank_skips", [])
    assert K in lp.resting
    assert lp.last_plan[K]["rank"] == pytest.approx((net50 - 0.75 * net50) / cap50)


# ------------------------------------------------------------- 3e calibration field
def test_finish_report_does_not_claim_inferred_calibration():
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame({"kind": "cash", "balance_delta_usd": 2, "fills_cash_usd": 0,
                 "settlements_usd": 0, "deposits_usd": 0})
    report = lp.finish()
    assert "calibration_inferred" not in report
    assert report["inferred_credits_excluded_n"] >= 0


# ------------------------------------------------------------- 3f positions
def test_status_exposes_per_market_positions():
    from mm.status_page import status_payload
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(trade(K, T0 + 2, "t1", 30, 5000, "no"))
    st = status_payload(lp.live_snapshot())
    assert st["positions"] == {K: {"yes": 100.0, "no": 0.0, "yes_cost": 40.0, "no_cost": 0.0}}


# ------------------------------------------------------------- replay fee default
def test_replay_default_charges_the_maker_fee(tmp_path):
    from decimal import Decimal
    from mm.recorder import Recorder
    from mm.replay import replay
    ts = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    path = tmp_path / "rec.jsonl"
    rec = Recorder(path)
    rec.book(ts=ts, market="KXBRENTD-1", yes_bid=40, no_bid=58, yes_size=0, no_size=0)
    rec.quote(ts=ts, market="KXBRENTD-1", side="yes", price_cents=40, size=10, order_id="p1")
    rec.trade(trade={"trade_id": "t1", "ticker": "KXBRENTD-1", "count_fp": "10.00",
                     "yes_price_dollars": "0.40", "no_price_dollars": "0.60",
                     "taker_side": "no", "created_time": "2026-10-01T00:00:05+00:00"})
    rec.close()
    assert replay(path).fees_usd > Decimal(0)   # missing fee_type: standard maker fee
    assert replay(path, fee_type="quadratic").fees_usd == Decimal(0)
