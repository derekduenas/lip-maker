"""Integration: the watchdog reads the real RunLoop /status shape -- per-market
positions for inventory, and fees/rebates in the session P&L."""
import pytest

from mm.safety import lip_watchdog as wd
from mm.status_page import status_payload
from tests.test_review_integ_loop import (  # noqa: F401  (autouse env fixture)
    K, PM, T0, _env, _pm_print, newloop, pm_loop, program, snap, trade,
)


def _kalshi_filled():
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(trade(K, T0 + 2, "t1", 30, 5000, "no"))
    assert lp.fills_total == 1
    return lp


def test_watchdog_inventory_uses_the_live_positions_of_a_real_runloop():
    lp = _kalshi_filled()
    status = status_payload(lp.live_snapshot())
    inv = wd.inventory_breakdown(status)
    assert inv["basis"] == "positions"
    # 100 unpaired YES bought at 40c: worst case loses the $40 cost
    assert inv["inventory_usd"] == pytest.approx(40.0)
    assert inv["unpaired_usd"] == pytest.approx(40.0)


def test_watchdog_session_pnl_charges_fees_from_the_real_status():
    lp = _kalshi_filled()
    status = status_payload(lp.live_snapshot())
    buckets = status["buckets"]
    markout = sum(b["markout_usd"] for b in buckets.values())
    fees = sum(b["fees_usd"] for b in buckets.values())
    assert fees > 0
    assert wd.session_pnl(status) == pytest.approx(markout - fees)


def test_watchdog_session_pnl_adds_pmus_rebates():
    lp = pm_loop()
    lp.on_frame(_pm_print(T0 + 2, "pmus:abc:1", 40, 50, "no"))
    status = status_payload(lp.live_snapshot())
    buckets = status["buckets"]
    rebates = sum(b["rebates_usd"] for b in buckets.values())
    assert rebates > 0
    markout = sum(b["markout_usd"] for b in buckets.values())
    assert wd.session_pnl(status) == pytest.approx(markout + rebates)


def test_session_pnl_without_fee_fields_is_unchanged():
    status = {"buckets": {"short": {"markout_usd": -3.0, "raw_est_usd": 1.0}}}
    assert wd.session_pnl(status) == -3.0
    assert wd.session_pnl(status, include_rewards=True) == -2.0
