"""Adversarial-review fixes for the pre-live ops layer and the go/no-go statistics.

Each test reproduces a defect confirmed by running it against 9ac4c20 (red), then passes."""
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from deploy import verify_deploy as V
from engine import lip_reconcile as LR
from mm.live_ops import coid as C
from mm.live_ops import deadman as D
from mm.live_ops import rate_budget as RB
from mm.live_ops import reconcile as RC
from mm.order_machine import OrderBook
from mm.types import ManagedOrder, OrderState, Side, VenueName, VenueOrderView
from mm.unattended import go_no_go as G
from mm.unattended import reward_recon as RR

ROOT = Path(__file__).resolve().parent.parent


def _local(book, coid, state=OrderState.RESTING, remaining=10.0, oid="V", ts=0.0, filled=0.0, price=40):
    book.add(ManagedOrder(client_order_id=coid, venue=VenueName.KALSHI, market="M", side=Side.YES,
                          price_cents=price, remaining=remaining, state=state, order_id=oid,
                          updated_ts=ts, filled=filled))


def _view(oid, coid, rem=10.0, price=40, status="resting"):
    return VenueOrderView(order_id=oid, client_order_id=coid, market="M", side=Side.YES,
                          price_cents=price, remaining=rem, status=status)


def _rec(book, orders, fills=None, clock=1000.0, **kw):
    return RC.Reconciler(book, fetch_orders=lambda: orders, fetch_fills=(lambda: fills) if fills is not None else None,
                         clock=lambda: clock, on_halt=lambda *a: None, **kw)


# ------------------------------------------------------------------ reconcile
def test_fresh_pending_new_survives_the_grace_and_becomes_resting_when_the_venue_shows_it():
    b = OrderBook()
    _local(b, "c1", OrderState.PENDING_NEW, oid="", ts=995.0)
    r1 = _rec(b, [], clock=1000.0).run_once()
    assert not r1["halt"] and b.get("c1").state == OrderState.PENDING_NEW      # not rejected inside the grace
    r2 = _rec(b, [_view("V1", "c1")], clock=1001.0).run_once()
    assert not r2["halt"] and r2["unexplained"] == []
    assert b.get("c1").state == OrderState.RESTING and [o.client_order_id for o in b.resting()] == ["c1"]


def test_pending_new_past_the_grace_is_lost_and_halts():
    b = OrderBook()
    _local(b, "c1", OrderState.PENDING_NEW, oid="", ts=900.0)
    rep = _rec(b, [], clock=1000.0).run_once()
    assert rep["halt"] and rep["unexplained"][0]["kind"] == "lost_order"


def test_an_old_already_applied_fill_does_not_explain_a_new_disappearance():
    b = OrderBook()
    _local(b, "c1", remaining=4.0, oid="V1", filled=6.0)             # the 6-lot fill is already in the book
    old = [{"order_id": "V1", "count": 6.0}]
    rep = _rec(b, [], fills=old).run_once()
    assert rep["halt"] and rep["unexplained"][0]["kind"] == "missing_at_venue"


def test_a_drop_with_no_new_fill_is_unexplained_but_a_new_fill_explains_it():
    b = OrderBook()
    _local(b, "c1", remaining=4.0, oid="V1", filled=6.0)
    rep = _rec(b, [_view("V1", "c1", rem=1.0)], fills=[{"order_id": "V1", "count": 6.0}]).run_once()
    assert rep["halt"] and rep["unexplained"][0]["kind"] == "qty_mismatch"
    b2 = OrderBook()
    _local(b2, "c1", remaining=4.0, oid="V1", filled=6.0)
    rep = _rec(b2, [_view("V1", "c1", rem=1.0)], fills=[{"order_id": "V1", "count": 9.0}]).run_once()
    assert not rep["halt"]                                           # 3 new lots filled: 4 -> 1 is explained


def test_fills_are_counted_only_once_across_runs():
    b = OrderBook()
    _local(b, "c1", remaining=10.0, oid="V1")
    fills = [{"order_id": "V1", "count": 4.0}]
    r = RC.Reconciler(b, fetch_orders=lambda: [_view("V1", "c1", rem=6.0)], fetch_fills=lambda: fills,
                      clock=lambda: 1000.0, on_halt=lambda *a: None)
    assert not r.run_once()["halt"]                                  # 10 -> 6 explained by the 4-lot fill
    r.fetch_orders = lambda: []                                      # then the order vanishes, no new fill
    assert r.run_once()["halt"]


def test_two_venue_orders_with_our_client_id_are_flagged():
    b = OrderBook()
    _local(b, "c3", oid="o3")
    rep = _rec(b, [_view("o3", "c3"), _view("o3b", "c3")]).run_once()
    assert rep["halt"] and any(u["kind"] == "duplicate_client_order_id" for u in rep["unexplained"])


def test_a_venue_price_change_is_reported_and_a_cancelled_row_is_not_resting():
    b = OrderBook()
    _local(b, "c1", oid="V1", price=40)
    rep = _rec(b, [_view("V1", "c1", price=55)]).run_once()
    assert rep["halt"] and any(u["kind"] == "price_mismatch" for u in rep["unexplained"])
    b = OrderBook()
    _local(b, "c2", OrderState.PENDING_CANCEL, oid="V2")
    rep = _rec(b, [_view("V2", "c2", status="canceled")]).run_once()
    assert not rep["halt"] and b.get("c2").state == OrderState.CANCELLED and b.resting() == []


# ---------------------------------------------------------------- go/no-go stats
CFG = {"min_events": 5, "target_edge_cents": 0.5, "reward_haircut": 0.5, "min_frozen_days": 3.0}


def test_equal_event_weights_cannot_hide_a_negative_contract_weighted_edge():
    ev = {f"a{i}": [1, 1.0, 2.0 * 1.0 / 100] for i in range(30)}               # +2c on 1 contract
    ev.update({f"b{i}": [1, 100.0, -1.5 * 100.0 / 100] for i in range(30)})    # -1.5c on 100 contracts
    st = G.event_stats(ev)
    assert st["pooled_cents"] < -1.0
    v = G.verdict(st, reward_cents_per_contract=0.0, frozen_days=9.0)
    assert v["verdict"] != "GO"


def test_zero_variance_does_not_give_go_on_a_zero_standard_error():
    ev = {f"e{i}": [1, 10.0, 0.0] for i in range(40)}                          # every event exactly 0c
    v = G.verdict(G.event_stats(ev), reward_cents_per_contract=0.0, frozen_days=9.0)
    assert v["verdict"] != "GO"
    ev = {f"e{i}": [1, 10.0, 0.1] for i in range(40)}                          # exactly +1c each, SD 0
    st = G.event_stats(ev)
    v = G.verdict(st, reward_cents_per_contract=0.0, frozen_days=9.0)
    assert v["edge_lower_90_cents"] < st["mean_cents"]                          # an SE floor is applied


def test_docstrings_do_not_overclaim_trials_or_alpha():
    assert "distinct" not in (G.note_params.__doc__ or "").lower() or "changes" in G.note_params.__doc__.lower()
    assert "alpha" in G.events_needed.__doc__.lower() and "10%" in G.verdict.__doc__


# ------------------------------------------------------------------ client ids
def test_a_torn_last_journal_line_does_not_swallow_the_next_intent(tmp_path):
    p = str(tmp_path / "j.jsonl")
    Path(p).write_text('{"coid": "LIP-a", "state": "intent"}\n{"coid": "LIP-torn", "sta')
    s = C.ClientOrderIds(p)
    cb = s.new("M", "yes")
    reborn = C.ClientOrderIds(p)
    assert reborn.state(cb) is not None and cb in reborn.unresolved()


def test_the_directory_is_fsynced_when_the_journal_is_first_created(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(C, "_fsync_dir", lambda d: seen.append(d))
    C.ClientOrderIds(str(tmp_path / "sub" / "j.jsonl")).new("M", "yes")
    assert seen and seen[0] == str(tmp_path / "sub")


def test_a_long_prefix_never_truncates_away_the_random_part(tmp_path):
    s = C.ClientOrderIds(str(tmp_path / "j.jsonl"))
    ids = {s.new("M", "yes", prefix="P" * 80) for _ in range(20)}
    assert len(ids) == 20 and all(len(i) <= 64 for i in ids)


def test_a_cancelled_lookup_is_not_marked_acked_and_a_duplicate_refusal_is_not_terminal(tmp_path):
    s = C.ClientOrderIds(str(tmp_path / "j.jsonl"))
    c = s.new("M", "yes")

    def send(_):
        raise C.AmbiguousResponse()
    out = C.submit_idempotent(s, c, send, lookup=lambda _: {"order_id": "V", "status": "canceled"},
                              sleep=lambda x: None)
    assert out["status"] != "acked_after_lookup" and s.state(c)["state"] == "cancelled"
    c2 = s.new("M", "yes")
    calls = []

    def send2(_):
        calls.append(1)
        if len(calls) == 1:
            raise C.AmbiguousResponse()
        raise C.DuplicateOrder()
    out = C.submit_idempotent(s, c2, send2, lookup=lambda _: None, sleep=lambda x: None)
    assert out["status"] == "unknown" and s.state(c2)["state"] == "unknown" and c2 in s.unresolved()


# ---------------------------------------------------------------- rate budget
def test_a_cancel_may_combine_the_cancel_and_write_lanes_and_batches_chunk():
    t = [0.0]
    rb = RB.RateBudget("basic", clock=lambda: t[0])
    assert rb.acquire("cancel", items=3, cost=10)                    # 30 of the ~52 cancel tokens
    assert rb.acquire("cancel", items=4, cost=10)                    # only 22 left there: tops up from the write lane
    assert rb.max_items("write") == 15 and rb.max_items("cancel") == 21
    assert rb.chunks("write", 40) == [15, 15, 10]
    assert rb.wait_time("write", items=40) == float("inf")           # callers must chunk


# --------------------------------------------------------------------- dead-man
def test_the_deadman_ping_runs_off_the_caller_thread_with_a_hard_deadline():
    gate = threading.Event()
    d = D.DeadMansSwitch("https://x/y", interval_s=0, fetch=lambda u, t: gate.wait(5))
    t0 = time.monotonic()
    assert d.ping_async(100.0, healthy=True)["reason"] == "started"
    assert time.monotonic() - t0 < 0.5
    assert d.ping_async(101.0, healthy=True)["reason"] == "inflight"
    gate.set()
    d.join(2.0)
    assert d.last_ok_ts == 100.0


# ------------------------------------------------------------------ deploy verify
def _ok_status(**kw):
    s = {"paper": True, "mode": "paper", "live_armed": False, "kill": None, "state": {}, "feed": {"connected": True},
         "last_frame_ts": 1000.0, "build": {"commit": "a" * 40}}
    s.update(kw)
    return s


def test_verify_deploy_rejects_nan_future_and_short_commits():
    base = dict(expect_commit="a" * 40, now=1010.0)
    assert V.check(_ok_status(), **base) == []
    assert V.check(_ok_status(last_frame_ts=float("nan")), **base)
    assert V.check(_ok_status(last_frame_ts=1010.0 + 86400), **base)
    assert V.check(_ok_status(build={"commit": "a"}), **base)
    assert V.check(_ok_status(), expect_commit="", now=1010.0) == ["no expected commit given"] or \
        V.check(_ok_status(), expect_commit=None, now=1010.0) == []
    assert V.check(_ok_status(build={"commit": "b" * 40}), **base)


def test_verify_deploy_main_refuses_an_empty_or_malformed_expected_commit(capsys):
    assert V.main(["--expect-commit", "", "--wait-s", "0"]) == 2
    assert V.main(["--expect-commit", "xyz", "--wait-s", "0"]) == 2


def test_deploy_script_has_no_eval_and_rejects_hostile_branch_names(tmp_path):
    src = (ROOT / "deploy" / "deploy_and_verify.sh").read_text()
    assert "eval" not in src
    marker = tmp_path / "pwned"
    env = dict(os.environ, LIP_DEPLOY_BRANCH=f"x'; touch {marker}; echo '")
    r = subprocess.run(["bash", str(ROOT / "deploy" / "deploy_and_verify.sh"), "--dry-run"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and not marker.exists()


# --------------------------------------------------------------- reward recon
def _est(market, pid, usd, start="2026-09-01T00:00:00Z", ts=None):
    row = {"market": market, "program_id": pid, "series": market.split("-")[0], "estimated_usd": str(usd),
           "period_start": start}
    if ts is not None:
        row["ts"] = ts
    return row


def _cred(market, pid, usd, start="2026-09-01T00:00:00Z"):
    return {"kind": "liquidity_reward", "source": sorted(__import__("engine.reward_provenance", fromlist=["x"]).PAID_SOURCES)[0],
            "market": market, "program_id": pid, "amount_usd": usd, "period_start": start}


def test_unpaid_estimates_lower_the_ratio():
    est = [_est(f"K{i}-X", f"p{i}", 2) for i in range(20)]
    cred = [_cred(f"K{i}-X", f"p{i}", 2) for i in range(10)]
    rep = RR.reconcile_periods(est, cred)
    assert float(rep["ratio"]) == pytest.approx(0.5)


def test_recent_unpaid_estimates_inside_the_payment_lag_are_not_counted_as_unpaid():
    now = 1_000_000.0
    est = [_est(f"K{i}-X", f"p{i}", 2, ts=now - 3600) for i in range(10)]          # archived an hour ago
    est += [_est(f"J{i}-X", f"q{i}", 2, ts=now - 10 * 86400) for i in range(10)]    # old enough to be due
    cred = [_cred(f"J{i}-X", f"q{i}", 2) for i in range(5)]
    rep = RR.reconcile_periods(est, cred, now=now, lag_s=3 * 86400)
    assert float(rep["ratio"]) == pytest.approx(0.5)


def test_credit_matches_on_period_start_and_paid_days_come_from_matched_credits_only():
    est = [_est("A-X", "p", 2, start="2026-10-01T00:00:00Z")]
    rep = RR.reconcile_periods(est, [_cred("A-X", "p", 2, start="2026-09-01T00:00:00Z")])
    assert rep["matched"] == 0 and rep["paid_days"] == 0
    unrelated = [_cred("Z-X", "z", 0, start=f"2026-09-0{d}T00:00:00Z") for d in (1, 2, 3)]
    assert RR.reconcile_periods(est, unrelated)["paid_days"] == 0


def test_one_program_with_several_periods_matches_each_period():
    est = [_est("A-X", "p", 2, start=f"2026-09-0{d}T00:00:00Z") for d in (1, 2)]
    cred = [_cred("A-X", "p", 1.5, start=f"2026-09-0{d}T00:00:00Z") for d in (1, 2)]
    rep = RR.reconcile_periods(est, cred)
    assert rep["matched"] == 2 and float(rep["paid_usd"]) == pytest.approx(3.0)


def test_non_finite_credit_amounts_are_rejected():
    for bad in ("Infinity", "NaN", "-Infinity", float("inf")):
        accepted, rejected = LR.credits_from_ledger([_cred("A-X", "p", bad)])
        assert accepted == [] and rejected[0]["reason"] == "bad_amount"


def test_nan_estimates_do_not_crash_the_report():
    rep = RR.reconcile_periods([_est("A-X", "p", "NaN"), _est("B-X", "q", "Infinity")], [_cred("A-X", "p", 1)])
    assert rep["matched"] == 0


def test_measured_haircut_never_loosens_the_operators():
    assert RR.effective_haircut(0.5, 0.2) == 0.5
    assert RR.effective_haircut(0.5, 0.8) == 0.8
    assert RR.effective_haircut(0.5, None) == 0.5
