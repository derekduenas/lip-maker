"""grok/lip-fixes-20261009: payable headline (a), relative starvation and
probe reserve (b), executable marks (c), inventory controls + paper exits (d),
tape-burst guard (e), payable selection (g), watchdog inventory/alerts (i)."""
import json
from decimal import Decimal

import pytest

from mm.unattended import loop as L
from tests.test_review_loop_pnl import (  # noqa: F401  (autouse env fixture)
    M, T0, _env, _filled_loop, newloop, program, snap, trade,
)

FEE_38 = 0.07 * 100 * 0.38 * 0.62


# ------------------------------------------------------------------ (c) marks
def test_walk_bids_never_assumes_depth_beyond_the_book():
    proceeds, filled, fills, short = L.walk_bids([(40, 30), (38, 50)], 100)
    assert filled == 80 and short == 20
    assert proceeds == pytest.approx(30 * 0.40 + 50 * 0.38)
    assert fills == [(40, 30), (38, 50)]


def test_executable_mark_uses_the_bid_minus_taker_fee_and_pairs_at_one_dollar():
    lp = _filled_loop()                                   # 100 YES @40
    lp.on_frame(snap(M, T0 + 4, [(38, 500)], [(60, 500)]))
    parts = lp.pnl_parts()
    assert parts["mark_basis"] == "executable"
    assert parts["markout_usd"] == pytest.approx(38.0 - FEE_38 - 40.0, abs=1e-6)
    assert parts["markout_mid_usd"] == pytest.approx(-1.0)
    # pair 60 of them: 60 pairs are worth exactly $60, only 40 YES are sold into the bid
    lp.position[M]["no"] = 60.0
    lp.position[M]["no_cost"] = 60 * 0.55
    v, src, short = lp._exec_value(M, lp.position[M])
    assert src == "exec_book" and short == 0
    assert v == pytest.approx(60.0 + 40 * 0.38 - 0.07 * 40 * 0.38 * 0.62, abs=1e-6)


def test_quote_mark_frame_marks_a_held_market_without_a_live_book(monkeypatch):
    lp = _filled_loop()
    lp.end_program(M)                                     # no program, no live book
    assert M not in lp.accruals
    assert M in lp.mark_candidates()
    lp.on_frame({"type": "quote_mark", "market": M, "ts": T0 + 10, "yes": [[30, 1000]], "no": [[65, 10]]})
    v, src, _ = lp._exec_value(M, lp.position[M])
    assert src == "exec_rest"
    assert v == pytest.approx(30.0 - 0.07 * 100 * 0.30 * 0.70, abs=1e-6)


def test_parse_kalshi_orderbook_shapes():
    fp = {"orderbook_fp": {"yes_dollars": [["0.4500", "12.00"], ["0.4600", "3.00"]],
                           "no_dollars": [["0.5000", "7.00"]]}}
    assert L.parse_kalshi_orderbook(fp) == ([(46, 3.0), (45, 12.0)], [(50, 7.0)])
    legacy = {"orderbook": {"yes": [[45, 12], [46, 3]], "no": None}}
    assert L.parse_kalshi_orderbook(legacy) == ([(46, 3.0), (45, 12.0)], [])


def test_mark_basis_change_rebases_the_daily_pnl(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setenv("LIP_MARK_BASIS", "mid")
    lp = _filled_loop()
    lp.attach_state(str(path))
    lp.on_frame(snap(M, T0 + 4, [(20, 500)], [(60, 500)]))   # wide book: bid far below mid
    lp.save_state(force=True)
    monkeypatch.setenv("LIP_MARK_BASIS", "executable")
    lp2 = newloop()
    lp2.attach_state(str(path))
    assert lp2.mark_basis_rebased == {"from": "mid", "to": "executable"}
    lp2.on_frame({"type": "clock", "ts": T0 + 5})
    assert float(lp2.daily_pnl_usd()) == pytest.approx(0.0, abs=1e-6)   # the switch is not a loss


# ------------------------------------------------------------------ (a) payable
def test_payable_twins_accumulate_and_backfill_from_period_estimates(tmp_path):
    lp = newloop()
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    for i in range(1, 60):
        lp.on_frame({"type": "clock", "ts": T0 + i})
    lp.on_frame(program(M, program_id="p2", start_ts=T0 + 60, end_ts=T0 + 8 * 86400))
    assert lp.closed_periods[M] > 0
    assert lp.closed_periods_payable[M] == 0.0            # under the $1 minimum
    # A pre-fix state file: no payable twins -> rebuilt from period_estimates.
    path = tmp_path / "s.json"
    lp.attach_state(str(path))
    lp.period_estimates.append({"market": "KXOLD-1", "series": "KXOLD", "estimated_usd": "2.50", "raw_usd": "2.7"})
    lp.save_state(force=True)
    data = json.loads(path.read_text())
    for k in ("closed_periods_payable", "closed_periods_payable_agg"):
        data.pop(k)
    path.write_text(json.dumps(data))
    lp2 = newloop()
    lp2.attach_state(str(path))
    assert lp2.payable_backfill["source"] == "period_estimates"
    assert lp2.closed_periods_payable_agg == {"kalshi/backfill": pytest.approx(2.5)}
    rep = lp2.pnl_report({})
    assert rep["pnl_parts"]["est_rewards_usd"] == pytest.approx(2.5)
    assert rep["pnl_parts"]["est_rewards_gross_usd"] > 0


# ------------------------------------------------------------------ (b) starvation / probe
def test_starvation_threshold_is_a_fraction_of_the_cap(monkeypatch):
    monkeypatch.setenv("LIP_STARVED_BELOW_FRAC", "0.25")
    monkeypatch.setenv("LIP_STARVED_BELOW_USD", "10")
    assert L.starved_below_usd(1000.0) == pytest.approx(250.0)
    assert L.starved_below_usd(20.0) == pytest.approx(10.0)
    lp = newloop(bankroll=1500.0)
    cap = float(lp.risk.limits.per_venue_usd) * L.alloc_cap_fraction()
    assert cap * 0.2 > 10.0                                # the relative rule binds, not the $10 floor
    lp._note_starvation(T0, reward_budget=cap * 0.2)
    assert lp._starved_since == T0                         # 20% of the cap < 25%: starved
    lp._note_starvation(T0 + 1, reward_budget=cap * 0.3)
    assert lp._starved_since is None


# ------------------------------------------------------------------ (d) inventory controls
def test_soft_cap_defaults_to_80pct_of_the_watchdog_limit(monkeypatch):
    monkeypatch.delenv("LIP_INV_SOFT_CAP_USD", raising=False)
    monkeypatch.setenv("LIP_WD_MAX_INVENTORY_USD", "500")
    assert L.inv_soft_cap_usd() == pytest.approx(400.0)
    monkeypatch.setenv("LIP_INV_SOFT_CAP_USD", "0")
    assert L.inv_soft_cap_usd() == 0.0


def test_soft_cap_band_and_near_resolution_block_only_adding_sides(monkeypatch):
    lp = _filled_loop()                                   # long 100 YES @40 = $40 unpaired
    ts = T0 + 5
    monkeypatch.setenv("LIP_INV_SOFT_CAP_USD", "30")
    assert lp._side_blocked(M, "yes", ts) == "inventory_soft_cap"
    assert lp._side_blocked(M, "no", ts) == ""            # reducing side stays open
    monkeypatch.setenv("LIP_INV_SOFT_CAP_USD", "0")
    monkeypatch.setenv("LIP_AVOID_BAND_LO", "30")
    monkeypatch.setenv("LIP_AVOID_BAND_HI", "90")
    assert lp._side_blocked(M, "yes", ts) == "price_band"
    monkeypatch.setenv("LIP_AVOID_BAND_HI", "0")
    monkeypatch.setenv("LIP_FLATTEN_BEFORE_EVENT_H", "6")
    close = lp._resolution_anchor(M)
    assert lp._side_blocked(M, "yes", close - 3 * 3600) == "near_resolution"
    assert lp._side_blocked(M, "no", close - 3 * 3600) == ""


def test_paper_exit_sells_aged_inventory_into_the_bid_with_taker_fee(monkeypatch):
    monkeypatch.setenv("LIP_EXITS_PAPER_TAKER", "1")
    monkeypatch.setenv("LIP_FLATTEN_AGE_H", "1")
    lp = _filled_loop()
    lp.on_frame(snap(M, T0 + 4, [(38, 60), (37, 500)], [(60, 500)]))
    before = lp.pnl_parts()["markout_usd"] - lp.fees_usd_total
    ts = T0 + 2 * 3600
    lp.now = ts
    assert lp._inventory_exits(ts) == 1
    pos = lp.position[M]
    assert pos["yes"] == pytest.approx(0.0)
    ex = lp.paper_exits[-1]
    assert ex["why"] == "aged" and ex["levels"] == [[38, 60.0], [37, 40.0]]
    fee = 0.07 * 60 * 0.38 * 0.62 + 0.07 * 40 * 0.37 * 0.63
    assert ex["proceeds_usd"] == pytest.approx(60 * 0.38 + 40 * 0.37)
    # The executable mark already valued the leg at the exit: P&L does not jump.
    after = lp.pnl_parts()["markout_usd"] - lp.fees_usd_total
    assert after == pytest.approx(before, abs=0.02)
    assert lp.paper_exit_stats["fees_usd"] == pytest.approx(fee, abs=0.02)
    assert float(lp.inv_committed.get(M, 0)) == pytest.approx(0.0)


def test_paper_exit_is_off_by_default_and_never_without_a_fresh_book(monkeypatch):
    monkeypatch.setenv("LIP_FLATTEN_AGE_H", "1")
    lp = _filled_loop()
    assert lp._inventory_exits(T0 + 2 * 3600) == 0        # LIP_EXITS_PAPER_TAKER unset
    monkeypatch.setenv("LIP_EXITS_PAPER_TAKER", "1")
    lp.end_program(M)
    lp.exec_book.pop(M, None)
    lp.now = T0 + 2 * 3600
    assert lp._inventory_exits(T0 + 2 * 3600) == 0
    assert lp.paper_exit_stats.get("no_book_n") == 1


# ------------------------------------------------------------------ (e) tape burst
def test_one_sided_public_tape_burst_pulls_the_quote(monkeypatch):
    monkeypatch.setenv("LIP_TAPE_BURST_ENABLE", "1")
    monkeypatch.setenv("LIP_TAPE_BURST_CONTRACTS", "250")
    lp = newloop()
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    assert M in lp.resting
    # taker YES buys at 45: above our 40 YES bid / does not trade through our NO at 55+45=100? (no fill)
    for i in range(3):
        lp.on_frame(trade(M, T0 + 2 + i, f"b{i}", 44, 100, "yes"))
    assert lp.tape_bursts_n == 1
    assert M not in lp.resting
    assert lp.pulls.get("tape_burst") == 1


def test_two_sided_tape_is_not_a_burst(monkeypatch):
    monkeypatch.setenv("LIP_TAPE_BURST_ENABLE", "1")
    lp = newloop()
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    for i in range(4):
        lp.on_frame(trade(M, T0 + 2 + i, f"c{i}", 44, 100, "yes" if i % 2 else "no"))
    assert lp.tape_bursts_n == 0


# ------------------------------------------------------------------ (g) payable selection
def test_payable_selection_zeroes_a_reward_that_cannot_reach_the_floor(monkeypatch):
    from mm.selector import quote_economics
    probe = newloop()
    probe.on_frame(program(M))
    probe.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    km0 = next(k for k in probe._markets() if k.market == M)
    share = quote_economics(km0, 100.0, sides=("yes", "no"))[2]
    assert share > 0
    # A pool where a full period at today's share pays ~$2 (passes the $1 floor
    # in reward_per_day), but at the measured-uptime haircut (0.5) only ~$1.
    pool = 2.0 / share / km0.seconds_left * km0.period_seconds
    lp = newloop()
    lp.on_frame(program(M, reward=pool))
    lp.on_frame(snap(M, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    km = next(k for k in lp._markets() if k.market == M)
    net, cap, share2, yc, nc = quote_economics(km, 100.0, sides=("yes", "no"))
    net0 = quote_economics(km, 100.0, sides=("yes", "no"), reward_factor=0.0)[0]
    monkeypatch.setenv("LIP_PAYABLE_SELECT", "0")
    assert lp._payable_net(km, 100.0, ("yes", "no"), net, share2) == net
    monkeypatch.setenv("LIP_PAYABLE_SELECT", "1")
    assert lp._payable_net(km, 100.0, ("yes", "no"), net, share2) == pytest.approx(net0)
    assert lp.payable_select_stats["below_floor"] == 1


# ------------------------------------------------------------------ (i) watchdog
def _wd(tmp_path, **env):
    from mm.safety import lip_watchdog as W
    base = {"LIP_WD_STATE_DIR": str(tmp_path), "LIP_WD_MAX_INVENTORY_USD": "500"}
    base.update(env)
    return W, W.Config(base)


def _status(unpaired_cost, locked_loss_pairs):
    # one unpaired market + one pair bought at 105c per pair (locked loss)
    return {"positions": {"A": {"yes": unpaired_cost / 0.5, "no": 0, "yes_cost": unpaired_cost, "no_cost": 0},
                          "B": {"yes": locked_loss_pairs, "no": locked_loss_pairs,
                                "yes_cost": 0.55 * locked_loss_pairs, "no_cost": 0.50 * locked_loss_pairs}}}


def test_watchdog_inventory_is_unpaired_only_and_warns_at_80pct(tmp_path):
    W, cfg = _wd(tmp_path)
    st = _status(450.0, 2000)                              # unpaired 450, locked loss $100
    reasons, info = W.evaluate(cfg, {}, 1e9, dict(st, session_elapsed_s=0), None, 1e9)
    assert not any(r.startswith("inventory:") for r in reasons)
    assert info["inventory_usd"] == pytest.approx(450.0)
    assert info["inventory"]["paired_locked_loss_usd"] == pytest.approx(100.0)
    assert info["inventory"]["inventory_with_locked_loss_usd"] == pytest.approx(550.0)
    assert info["inventory_warn"].startswith("inventory:450.00")
    W2, cfg2 = _wd(tmp_path, LIP_WD_INVENTORY_INCLUDE_LOCKED_LOSS="1")
    reasons, _ = W2.evaluate(cfg2, {}, 1e9, dict(st, session_elapsed_s=0), None, 1e9)
    assert any(r.startswith("inventory:550.00") for r in reasons)


def test_watchdog_forwards_new_engine_alerts_only(tmp_path):
    W, cfg = _wd(tmp_path, LIP_WD_ALERT_MIN_INTERVAL_S="0")
    log = tmp_path / "alerts-engine.log"
    log.write_text("2026-10-09T00:00:00+00:00  WARNING   lip_unattended  old line\n")
    state = {}
    assert W.forward_engine_alerts(cfg, state, 1e9) == 0      # first run starts at the end
    with log.open("a") as fh:
        fh.write("2026-10-09T00:01:00+00:00  INFO      lip_unattended  ignore me warn\n")
        fh.write("2026-10-09T00:02:00+00:00  CRITICAL  lip_unattended  kill latched\n")
    assert W.forward_engine_alerts(cfg, state, 1e9 + 1) == 1
    rec = json.loads((tmp_path / "alerts.log").read_text().strip().splitlines()[-1])
    assert rec["level"] == "ENGINE_CRITICAL" and "kill latched" in rec["message"]
    assert W.forward_engine_alerts(cfg, state, 1e9 + 2) == 0


# ------------------------------------------------------------------ (f) scorer is in test_lip_scorer.py
def test_paper_only_interlock_is_unchanged():
    assert L.resolve_mode({"LIP_PAPER": "true"}) == "paper"
    with pytest.raises(Exception):
        L.resolve_mode({"LIP_PAPER": "false"})
