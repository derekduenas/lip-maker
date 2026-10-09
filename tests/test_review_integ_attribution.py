"""Phase 4: estimated P&L attribution. The components must sum to the
engine's existing pnl_usd."""
from decimal import Decimal

import pytest

from mm.unattended.health import render_daily_summary
from tests.test_review_integ_loop import (  # noqa: F401  (autouse env fixture)
    K, PM, T0, _env, _pm_print, newloop, pm_loop, program, snap, trade,
)

PARTS = ("spread_capture_usd", "adverse_selection_usd", "inventory_mtm_usd",
         "est_rewards_kalshi_usd", "est_rewards_pmus_usd", "rebates_usd", "fees_usd")


def _sum(attr):
    return sum(attr[k] for k in PARTS)


def _kalshi_scenario():
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))   # mid 42.5
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(trade(K, T0 + 2, "t1", 30, 5000, "no"))                           # 100 YES @40
    lp.on_frame(snap(K, T0 + 3, [(10, 500)], [(85, 500)]))                         # mid 12.5
    lp.on_frame({"type": "clock", "ts": T0 + 2 + 600})
    return lp


def test_components_match_the_scenario_and_sum_to_pnl():
    lp = _kalshi_scenario()
    st = lp.live_snapshot(accrual=lp.live_accrual())
    attr = st["pnl_attribution"]
    assert "estimate (paper)" in attr["label"]
    assert attr["spread_capture_usd"] == pytest.approx(100 * (42.5 - 40) / 100.0)
    assert attr["adverse_selection_usd"] == pytest.approx(100 * (12.5 - 42.5) / 100.0)
    # grok fix (c): executable marks. The held 100 YES are worth the 10c bid
    # minus the taker fee, not the 12.5c mid: inventory_mtm carries that gap.
    fee = 0.07 * 100 * 0.10 * 0.90
    assert st["pnl_parts"]["markout_mid_usd"] == pytest.approx(100 * (12.5 - 40) / 100.0)
    assert st["pnl_parts"]["markout_usd"] == pytest.approx(100 * (10 - 40) / 100.0 - fee, abs=1e-6)
    assert attr["inventory_mtm_usd"] == pytest.approx(-(2.5 + fee), abs=1e-6)
    assert attr["fees_usd"] < 0                      # Kalshi maker fee is a cost
    # grok fix (a): headline rewards are payable (10 min of accrual < $1 -> 0); gross kept.
    assert attr["est_rewards_kalshi_usd"] == 0
    assert attr["est_rewards_gross_kalshi_usd"] > 0
    assert attr["est_rewards_pmus_usd"] == 0
    assert _sum(attr) == pytest.approx(float(Decimal(st["pnl_usd"])), abs=1e-5)
    assert attr["total_usd"] == pytest.approx(float(Decimal(st["pnl_usd"])), abs=1e-6)


def test_identity_holds_with_settlement_rebates_and_partial_rewards():
    lp = _kalshi_scenario()
    lp.settle(K, "no")                                # MTM remainder moves to settlement
    for st in (lp.live_snapshot(), lp.live_snapshot(accrual=lp.live_accrual())):
        attr = st["pnl_attribution"]
        assert _sum(attr) == pytest.approx(float(Decimal(st["pnl_usd"])), abs=1e-5)
    pm = pm_loop()
    pm.on_frame(_pm_print(T0 + 2, "pmus:abc:1", 40, 50, "no"))
    st = pm.live_snapshot(accrual=pm.live_accrual())
    attr = st["pnl_attribution"]
    assert attr["rebates_usd"] > 0 and attr["fees_usd"] == 0
    assert attr["est_rewards_pmus_usd"] >= 0 and attr["est_rewards_kalshi_usd"] == 0
    assert _sum(attr) == pytest.approx(float(Decimal(st["pnl_usd"])), abs=1e-5)


def test_identity_holds_in_the_finish_report_and_status_payload():
    from mm.status_page import status_payload
    lp = _kalshi_scenario()
    rep = lp.finish()
    assert _sum(rep["pnl_attribution"]) == pytest.approx(float(Decimal(rep["pnl_usd"])), abs=1e-5)
    st = status_payload(lp.live_snapshot())
    assert "pnl_attribution" in st


def test_daily_summary_prints_the_attribution_as_an_estimate():
    lp = _kalshi_scenario()
    st = lp.live_snapshot(accrual=lp.live_accrual())
    text = render_daily_summary(day="2026-10-01", fills=1, pnl_usd=float(st["pnl_usd"]),
                                rewards_usd=0.0, attribution=st["pnl_attribution"])
    line = [x for x in text.splitlines() if x.startswith("pnl_attribution")][0]
    assert "estimate_paper" in line
    for key in ("spread_capture_usd", "adverse_selection_usd", "inventory_mtm_usd",
                "est_rewards_kalshi_usd", "est_rewards_pmus_usd", "rebates_usd", "fees_usd"):
        assert key in line


def test_mid_basis_keeps_the_old_attribution(monkeypatch):
    monkeypatch.setenv("LIP_MARK_BASIS", "mid")
    lp = _kalshi_scenario()
    st = lp.live_snapshot(accrual=lp.live_accrual())
    attr = st["pnl_attribution"]
    assert attr["inventory_mtm_usd"] == pytest.approx(0.0, abs=1e-6)
    assert _sum(attr) == pytest.approx(float(Decimal(st["pnl_usd"])), abs=1e-5)
