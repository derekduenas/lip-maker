"""Review fixes, paper P&L honesty: outage accrual, MTM marks, maker fees,
max_reward, status pnl vs premium, rolled-over periods."""
import asyncio
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from mm.unattended import loop as L

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc).timestamp()
M = "KXCPI-26OCT30-T3"


POLICY = Path(__file__).resolve().parent.parent / "deploy/apex/lip-unattended.service.d/policy.conf"


def apply_policy(monkeypatch):
    """The deployed paper policy (policy.conf Environment= lines)."""
    for line in POLICY.read_text().splitlines():
        m = re.match(r"Environment=(\w+)=(.*)", line.strip())
        if m:
            monkeypatch.setenv(m.group(1), m.group(2))
    for name in ("LIP_PMUS_PAPER_ENABLE", "LIP_FV_ENABLE", "LIP_RECORD_ENABLE"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("LIP_SELECTION_DUMP", "off")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    apply_policy(monkeypatch)
    import monitor.alerts
    monkeypatch.setattr(monitor.alerts, "alert", lambda *a, **k: None)
    for name in ("LIP_MARKET_INV_CAP_USD", "LIP_EVENT_INV_CAP_USD", "LIP_SINGLE_FILL_CAP_USD",
                 "LIP_SKEW_ENABLE", "LIP_FILL_COOLDOWN_S", "LIP_CROSS_GUARD", "LIP_WD_MAX_CAPITAL_USD",
                 "LIP_SIZE_LADDER", "LIP_EVENT_CAP_FRAC", "LIP_FV_ENABLE", "LIP_PULL_MOVE_CENTS",
                 "LIP_REPEG_MIN_S", "LIP_EVENT_WINDOW_HOURS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIP_SELECTION_DUMP", "off")


def program(market, event=None, reward=500.0, target=300, **extra):
    row = {"kind": "program", "market": market, "series": market.split("-")[0],
           "program_id": "p-" + market, "period_reward_usd": reward, "period_seconds": 7 * 86400,
           "discount_factor": 0.5, "target_size": target, "start_ts": T0 - 3600, "end_ts": T0 + 7 * 86400,
           "close_ts": T0 + 30 * 86400, "days_to_settle": 30, "days_from_close": True,
           "exchange_index": 1, "category": "Economics", "event_ticker": event or market.rsplit("-", 1)[0],
           "rank_score": 1.0, "rank_penalty_per_day": 0.0}
    row.update(extra)
    return row


def snap(market, ts, yes, no):
    f = lambda lv: [[f"{p/100:.4f}", f"{q:.2f}"] for p, q in lv]
    return {"type": "orderbook_snapshot", "sid": None, "seq": None, "ts": ts,
            "msg": {"market_ticker": market, "yes_dollars_fp": f(yes), "no_dollars_fp": f(no)}}


def trade(market, ts, tid, yes_c, count, taker):
    return {"type": "trade", "ts": ts, "trade": {
        "trade_id": tid, "ticker": market, "count_fp": f"{count:.2f}",
        "yes_price_dollars": f"{yes_c/100:.4f}", "no_price_dollars": f"{(100-yes_c)/100:.4f}",
        "taker_side": taker, "created_time": datetime.fromtimestamp(ts, timezone.utc).isoformat()}}


def newloop(**kw):
    kw.setdefault("bankroll", 5000.0)
    return L.RunLoop(mode="paper", carry_forward=True, first_select_warmup_s=0, **kw)


def _filled_loop(fee_type=None):
    lp = newloop()
    extra = {} if fee_type is None else {"fee_type": fee_type}
    lp.on_frame(program(M, **extra))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert M in lp.resting
    lp.on_frame(trade(M, T0 + 2, "t1", 30, 5000, "no"))
    assert [(f["side"], f["price_cents"]) for f in lp.fills] == [("yes", 40)]
    return lp


# ------------------------------------------------------------------ 1. outage
def test_disconnect_frame_stops_carry_forward_over_an_outage():
    lp = newloop()
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert M in lp.resting
    lp.on_frame({"kind": "disconnect", "ts": T0 + 30, "reason": "ConnectionError"})
    assert not lp.connected and lp.accruals[M].book.book.stale
    assert M not in lp.resting  # nothing can be seen to fill while blind
    lp.on_frame({"kind": "reconnect", "ts": T0 + 899, "stale_s": 898.0})
    lp.on_frame(snap(M, T0 + 900, [(20, 50)], [(75, 50)]))
    a = lp.live_accrual([M])[M]
    # outage seconds are never scored: quotes were pulled (idle) or the book
    # was stale (unknown) -- not 900 carried-forward "known" seconds
    assert a["known"] <= 2
    assert a["unknown"] + a["idle"] >= 897


def test_drive_readonly_books_marks_disconnect_on_error_and_clean_close(tmp_path, monkeypatch):
    from tests.test_readonly_backoff import _src
    lp = newloop()
    calls = []

    async def fake_session(source, key, session, on_frame, state, **kw):
        calls.append(1)
        if len(calls) == 1:
            on_frame(program(M))
            on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
            on_frame({"type": "clock", "ts": T0 + 1})
            state["frames"] = True
            state["last_wall"] = time.time()
            raise ConnectionError("reset by peer")
        if len(calls) == 2:
            on_frame(snap(M, T0 + 900, [(20, 50)], [(75, 50)]))
            state["frames"] = True
            return  # clean close (1000/1001): async for just ends
        from mm.venues.readonly import ReadOnlyViolation
        raise ReadOnlyViolation("stop test")

    async def nap(_s):
        return None

    seen = []

    def on_frame(row):
        seen.append(row.get("kind") or row.get("type"))
        lp.on_frame(row)

    monkeypatch.setattr(L, "_readonly_books_session", fake_session)
    from mm.venues.readonly import ReadOnlyViolation
    with pytest.raises(ReadOnlyViolation):
        asyncio.run(L.drive_readonly_books(_src(tmp_path), on_frame, sleep=nap))
    # a clean close is a reconnect in the same loop, not the end of the driver
    assert len(calls) == 3
    assert seen.count("disconnect") == 2 and seen.count("reconnect") == 2
    a = lp.live_accrual([M])[M]
    assert a["known"] <= 2 and a["unknown"] + a["idle"] >= 897
    assert lp.kill is None  # a short blip pulls quotes, it does not latch


# ------------------------------------------------------------------ 2. MTM
def test_markout_keeps_position_when_book_goes_one_sided_or_empty():
    lp = _filled_loop()
    lp.on_frame(snap(M, T0 + 4, [], [(99, 5000)]))  # resolves NO: YES bids vanish
    b = lp.bucket_report()
    total = b["short"]["markout_usd"] + b["durable"]["markout_usd"]
    assert total == pytest.approx(100 * (1 - 40) / 100.0)
    lp.on_frame(snap(M, T0 + 5, [], []))  # whole book clears: last mark stays
    b = lp.bucket_report()
    assert b["short"]["markout_usd"] + b["durable"]["markout_usd"] == pytest.approx(-39.0)
    assert lp.markout_summary()["unpaired_usd"] == pytest.approx(40.0)


def test_settlement_frame_books_settlement_pnl_and_releases_inventory():
    lp = _filled_loop()
    assert float(lp.risk.market_usd[M]) >= 40.0 - 1e-9
    lp.on_frame({"kind": "settlement", "market": M, "result": "no", "ts": T0 + 10})
    b = lp.bucket_report()
    assert b["short"]["markout_usd"] + b["durable"]["markout_usd"] == pytest.approx(-40.0)
    assert float(lp.inv_committed.get(M, 0)) == 0.0
    assert M not in lp.live_snapshot()["unsettled_positions"]


def test_unsettled_position_after_close_is_listed():
    lp = _filled_loop()
    lp.programs[M].close_ts = T0 + 3
    lp.now = T0 + 100
    assert M in lp.live_snapshot()["unsettled_positions"]


# ------------------------------------------------------------------ 3. fees
@pytest.mark.parametrize("fee_type,expected", [
    ("quadratic_with_maker_fees", Decimal("0.42")),   # 0.0175 x 100 x 0.4 x 0.6
    (None, Decimal("0.42")),                          # missing -> standard maker fee
    ("quadratic", Decimal("0")),
])
def test_maker_fee_charged_per_paper_fill(fee_type, expected):
    from mm.accounting import kalshi_fee_usd
    lp = _filled_loop(fee_type)
    want = kalshi_fee_usd(40, 100.0, fee_type=fee_type or "quadratic_with_maker_fees")
    assert want == expected
    b = lp.bucket_report()
    fees = b["short"]["fees_usd"] + b["durable"]["fees_usd"]
    assert fees == pytest.approx(float(expected))
    pnl = b["short"]["pnl_usd"] + b["durable"]["pnl_usd"]
    markout = b["short"]["markout_usd"] + b["durable"]["markout_usd"]
    raw = b["short"]["raw_est_usd"] + b["durable"]["raw_est_usd"]
    assert pnl == pytest.approx(markout + raw - float(expected))


# ------------------------------------------------------------------ 4. max_reward
def test_max_reward_per_account_reaches_the_accrual():
    payload = {"incentive_programs": [{
        "id": "prog-1", "market_ticker": M, "period_reward": 5_000_000, "discount_factor_bps": 5000,
        "target_size_fp": "300", "start_date": "2026-10-01T00:00:00Z", "end_date": "2026-10-08T00:00:00Z",
        "max_reward_per_account": 25_000}]}
    frames = L._programs_from_incentive(payload)
    assert frames[0]["max_reward_usd"] == pytest.approx(2.5)
    lp = newloop()
    lp.on_frame(frames[0])
    assert lp.accruals[M].max_reward_usd == Decimal("2.5")
    assert L._program_sig(frames[0]) != L._program_sig(dict(frames[0], max_reward_usd=None))


def test_estimated_rewards_in_pnl_are_capped_at_max_reward():
    lp = newloop()
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    for i in range(1, 30):
        lp.on_frame({"type": "clock", "ts": T0 + i})
    # The cap reaches the accrual on a same-window re-feed. (A program capped
    # at ~$0 from the start is no longer selected at all: selection honours
    # the cap, tests/test_audit_2026_10_04.py.)
    lp.on_frame(program(M, max_reward_usd=0.000001))
    acc = lp.live_accrual([M])
    assert acc[M]["raw_usd"] > Decimal("0.000001")
    rep = lp.pnl_report(acc)
    assert rep["pnl_parts"]["est_rewards_gross_usd"] == pytest.approx(0.000001)
    assert rep["pnl_parts"]["est_rewards_usd"] == 0      # payable: under the $1 minimum


# ------------------------------------------------------------------ 5. status pnl
def test_status_pnl_is_not_minus_premium():
    lp = _filled_loop()
    lp.on_frame(snap(M, T0 + 4, [(38, 500)], [(60, 500)]))  # mid 39
    snap_ = lp.live_snapshot(accrual=lp.live_accrual([M]))
    assert Decimal(snap_["premium_paid_usd"]) == Decimal("40")
    parts = snap_["pnl_parts"]
    assert parts["markout_mid_usd"] == pytest.approx(100 * (39 - 40) / 100.0)
    # grok fix (c): executable mark = the 38c bid minus the taker fee.
    assert parts["markout_usd"] == pytest.approx(100 * (38 - 40) / 100.0 - 0.07 * 100 * 0.38 * 0.62, abs=1e-6)
    expected = parts["markout_usd"] + parts["est_rewards_usd"] + parts["rebates_usd"] - parts["fees_usd"]
    assert float(snap_["pnl_usd"]) == pytest.approx(expected, abs=1e-6)
    assert float(snap_["pnl_usd"]) > -5.0  # not -40 (minus the premium); bid-marked incl. taker fee
    assert "ESTIMATE" in snap_["pnl_usd_note"]
    from mm.status_page import status_payload
    out = status_payload(snap_)
    assert out["premium_paid_usd"] == snap_["premium_paid_usd"] and out["pnl_parts"] == parts


# ------------------------------------------------------------------ 6. rolled periods
def test_rolled_over_periods_stay_in_status_totals():
    lp = newloop()
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    for i in range(1, 60):
        lp.on_frame({"type": "clock", "ts": T0 + i})
    before = float(lp.live_accrual([M])[M]["raw_usd"])
    assert before > 0
    lp.on_frame(program(M, program_id="p2", start_ts=T0 + 60, end_ts=T0 + 8 * 86400))
    snap_ = lp.live_snapshot(accrual=lp.live_accrual([M]))
    assert snap_["closed_periods_n"] == 1
    assert snap_["closed_periods_raw_usd"] == pytest.approx(before, abs=1e-6)
    assert snap_["pnl_parts"]["est_rewards_gross_usd"] >= before - 1e-9
    assert snap_["pnl_parts"]["est_rewards_usd"] == 0    # payable: one minute is under $1
    b = snap_["buckets"]
    assert b["short"]["raw_est_usd"] + b["durable"]["raw_est_usd"] >= before - 1e-9
