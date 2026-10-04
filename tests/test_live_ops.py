"""Gaps 8 and 10: the pre-live operations layer, exercised ONLY against fakes.

Nothing here can reach an exchange: every module takes callables. Live trading stays
disabled; this is the safety net that must exist before any live order is considered."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from mm.live_ops import coid as C
from mm.live_ops import deadman as D
from mm.live_ops import order_groups as G
from mm.live_ops import rate_budget as RB
from mm.live_ops import reconcile as RC
from mm.order_machine import OrderBook
from mm.types import ManagedOrder, OrderState, Side, VenueName, VenueOrderView

ROOT = Path(__file__).resolve().parent.parent


# ============================================================ retry-safe client order ids
def test_ids_are_unique_short_and_persisted_before_they_are_returned(tmp_path):
    store = C.ClientOrderIds(str(tmp_path / "coid.jsonl"))
    ids = {store.new("KXCPI-26OCT30-T3", "yes") for _ in range(200)}
    assert len(ids) == 200 and all(len(i) <= 64 and i.startswith("LIP-") for i in ids)
    lines = (tmp_path / "coid.jsonl").read_text().splitlines()
    assert len(lines) == 200 and json.loads(lines[0])["state"] == "intent"


def test_the_id_is_on_disk_before_the_send_happens(tmp_path):
    store = C.ClientOrderIds(str(tmp_path / "coid.jsonl"))
    coid = store.new("M", "yes")
    seen = []

    def send(c):
        seen.append((tmp_path / "coid.jsonl").read_text())
        return {"order_id": "V1"}

    C.submit_idempotent(store, coid, send, lookup=lambda c: None, sleep=lambda s: None)
    assert coid in seen[0]                                   # journalled before the first send


def test_a_crash_between_send_and_ack_leaves_the_id_unresolved_after_restart(tmp_path):
    path = str(tmp_path / "coid.jsonl")
    store = C.ClientOrderIds(path)
    coid = store.new("M", "yes")
    store.mark(coid, "sent")
    reborn = C.ClientOrderIds(path)                          # process restarted
    assert reborn.unresolved() == [coid] and reborn.state(coid)["state"] == "sent"
    reborn.mark(coid, "acked", order_id="V9")
    assert C.ClientOrderIds(path).unresolved() == []


def test_a_timeout_is_resolved_by_lookup_and_never_resends(tmp_path):
    store = C.ClientOrderIds(str(tmp_path / "c.jsonl"))
    coid = store.new("M", "yes")
    calls = []

    def send(c):
        calls.append(c)
        raise C.AmbiguousResponse("timeout")

    out = C.submit_idempotent(store, coid, send, lookup=lambda c: {"order_id": "V1", "client_order_id": c},
                              sleep=lambda s: None)
    assert out["status"] == "acked_after_lookup" and calls == [coid]
    assert store.state(coid)["state"] == "acked"


def test_an_absent_order_is_resent_with_the_same_id_only(tmp_path):
    store = C.ClientOrderIds(str(tmp_path / "c.jsonl"))
    coid = store.new("M", "yes")
    calls = []

    def send(c):
        calls.append(c)
        if len(calls) < 3:
            raise C.AmbiguousResponse("503")
        return {"order_id": "V1"}

    out = C.submit_idempotent(store, coid, send, lookup=lambda c: None, sleep=lambda s: None)
    assert out["status"] == "acked" and out["attempts"] == 3 and set(calls) == {coid}


def test_when_lookup_itself_fails_nothing_is_resent_and_the_id_stays_unresolved(tmp_path):
    store = C.ClientOrderIds(str(tmp_path / "c.jsonl"))
    coid = store.new("M", "yes")
    calls = []

    def send(c):
        calls.append(c)
        raise C.AmbiguousResponse("timeout")

    def lookup(c):
        raise OSError("venue unreachable")

    out = C.submit_idempotent(store, coid, send, lookup=lookup, sleep=lambda s: None)
    assert out["status"] == "unknown" and len(calls) == 1 and store.unresolved() == [coid]


def test_a_rejection_is_final_and_exhausted_retries_end_unknown(tmp_path):
    store = C.ClientOrderIds(str(tmp_path / "c.jsonl"))
    coid = store.new("M", "yes")

    def reject(c):
        raise C.RejectedOrder("post_only_cross")

    out = C.submit_idempotent(store, coid, reject, lookup=lambda c: None, sleep=lambda s: None)
    assert out["status"] == "rejected" and store.state(coid)["state"] == "rejected"
    c2 = store.new("M", "no")
    out2 = C.submit_idempotent(store, c2, lambda c: (_ for _ in ()).throw(C.AmbiguousResponse("x")),
                               lookup=lambda c: None, sleep=lambda s: None, max_attempts=2)
    assert out2["status"] == "unknown" and out2["attempts"] == 2


# ============================================================ rate budget
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_budget_runs_at_a_fraction_of_the_tier_limit_with_a_burst():
    clk = Clock()
    rb = RB.RateBudget("basic", fraction=0.7, burst_s=3.0, clock=clk)
    assert rb.rates()["write_tps"] == pytest.approx(100 * 0.7 * 0.75)         # 25% of writes reserved for cancels
    n = 0
    while rb.acquire("write"):
        n += 1
    assert n == int(100 * 0.7 * 0.75 * 3.0 / 10)                                # 3 s of burst at cost 10
    clk.t += 1.0
    assert rb.acquire("write")                                                  # refilled


def test_cancels_still_get_through_when_writes_are_exhausted():
    clk = Clock()
    rb = RB.RateBudget("basic", clock=clk)
    while rb.acquire("write"):
        pass
    assert rb.acquire("cancel") and rb.acquire("cancel")
    got = 0
    while rb.acquire("cancel"):
        got += 1
    assert got >= 1
    assert not rb.acquire("write")


def test_batches_cost_per_item_and_oversized_requests_never_pass():
    rb = RB.RateBudget("basic", clock=Clock())
    assert rb.acquire("write", items=3) and rb.wait_time("write", items=10_000) == float("inf")
    assert not rb.acquire("write", items=10_000)


def test_wait_time_is_exact_and_validation_is_strict():
    clk = Clock()
    rb = RB.RateBudget("basic", clock=clk)
    while rb.acquire("read"):
        pass
    w = rb.wait_time("read")
    assert 0 < w < 1
    clk.t += w + 1e-9
    assert rb.acquire("read")
    with pytest.raises(ValueError):
        rb.acquire("teleport")
    with pytest.raises(ValueError):
        RB.RateBudget("platinum")
    with pytest.raises(ValueError):
        RB.RateBudget("basic", fraction=1.5)


# ============================================================ order groups
def test_group_trips_on_the_rolling_window_and_cancels_every_member():
    g = G.OrderGroupModel(limit_contracts=100, window_s=15.0)
    for c in ("a", "b", "c"):
        assert g.admit(c)
    assert g.on_fill(60, ts=0.0) == []
    assert g.on_fill(30, ts=10.0) == []
    assert sorted(g.on_fill(20, ts=14.0)) == ["a", "b", "c"]                    # 110 within 15 s
    assert g.triggered and not g.admit("d")
    g.reset()
    assert g.admit("d") and not g.triggered


def test_old_fills_age_out_of_the_window():
    g = G.OrderGroupModel(limit_contracts=100, window_s=15.0)
    g.admit("a")
    assert g.on_fill(90, ts=0.0) == []
    assert g.on_fill(90, ts=16.0) == []                                          # the first aged out
    assert not g.triggered


def test_lowering_the_limit_can_trigger_immediately_and_the_limit_is_clamped():
    g = G.OrderGroupModel(limit_contracts=100)
    g.admit("a")
    g.on_fill(50, ts=0.0)
    assert g.update_limit(40, ts=1.0) == ["a"] and g.triggered
    assert G.clamp_limit(0) == 1 and G.clamp_limit(5_000_000) == 1_000_000


def test_fake_adapter_mirrors_the_group_interface():
    ad = G.FakeOrderGroupAdapter()
    gid = ad.create(limit_contracts=50)
    assert ad.add_order(gid, "x") and ad.add_order(gid, "y")
    assert ad.fill(gid, 60, ts=0.0) == ["x", "y"]
    assert not ad.add_order(gid, "z")
    ad.reset(gid)
    assert ad.add_order(gid, "z")


# ============================================================ reconcile
def _local(book, coid, state=OrderState.RESTING, remaining=10.0, order_id="V", price=40, ts=0.0):
    o = ManagedOrder(client_order_id=coid, venue=VenueName.KALSHI, market="M", side=Side.YES, price_cents=price,
                     remaining=remaining, state=state, order_id=order_id, updated_ts=ts)
    book.add(o)
    return o


def _view(order_id, coid, remaining=10.0, price=40):
    return VenueOrderView(order_id=order_id, client_order_id=coid, market="M", side=Side.YES, price_cents=price,
                          remaining=remaining, status="resting")


def _rec(book, orders, fills=None, **kw):
    halts = []
    kw.setdefault("clock", lambda: 1000.0)
    r = RC.Reconciler(book, fetch_orders=lambda: orders, fetch_fills=lambda: fills or [],
                      on_halt=lambda why, rep: halts.append(why), **kw)
    return r, halts


def test_a_clean_book_reconciles_without_a_halt():
    b = OrderBook()
    _local(b, "c1", order_id="V1")
    r, halts = _rec(b, [_view("V1", "c1")])
    rep = r.run_once()
    assert rep["halt"] is False and rep["unexplained"] == [] and halts == []


def test_an_order_missing_at_the_venue_without_a_fill_is_unexplained_and_halts():
    b = OrderBook()
    _local(b, "c1", order_id="V1")
    r, halts = _rec(b, [])
    rep = r.run_once()
    assert rep["halt"] and rep["unexplained"][0]["kind"] == "missing_at_venue" and len(halts) == 1
    assert b.get("c1").state == OrderState.CANCELLED                              # venue truth still applied


def test_a_fill_explains_a_missing_order_and_a_landed_cancel_is_expected():
    b = OrderBook()
    _local(b, "c1", order_id="V1", remaining=10)
    _local(b, "c2", order_id="V2", state=OrderState.PENDING_CANCEL)
    r, halts = _rec(b, [], fills=[{"order_id": "V1", "count": 10.0, "ts": 990.0}])
    rep = r.run_once()
    assert rep["halt"] is False and {e["key"] for e in rep["explained"]} == {"c1", "c2"}


def test_an_unknown_venue_order_halts_and_is_adopted_so_it_can_be_cancelled():
    b = OrderBook()
    r, halts = _rec(b, [_view("V7", "stranger")])
    rep = r.run_once()
    assert rep["halt"] and rep["unexplained"][0]["kind"] == "unknown_venue_order"
    assert b.get("stranger") is not None and b.get("stranger").state == OrderState.RESTING


def test_quantity_change_needs_a_matching_fill():
    b = OrderBook()
    _local(b, "c1", order_id="V1", remaining=10)
    r, _h = _rec(b, [_view("V1", "c1", remaining=6)], fills=[{"order_id": "V1", "count": 4.0, "ts": 995.0}])
    assert r.run_once()["halt"] is False
    b2 = OrderBook()
    _local(b2, "c1", order_id="V1", remaining=10)
    r2, _h2 = _rec(b2, [_view("V1", "c1", remaining=6)])
    rep = r2.run_once()
    assert rep["halt"] and rep["unexplained"][0]["kind"] == "qty_mismatch"


def test_a_fresh_pending_new_is_given_a_grace_and_an_old_one_is_a_lost_order():
    b = OrderBook()
    _local(b, "c1", state=OrderState.PENDING_NEW, order_id="", ts=995.0)
    r, _h = _rec(b, [], fill_grace_s=30.0)
    assert r.run_once()["halt"] is False
    b2 = OrderBook()
    _local(b2, "c1", state=OrderState.PENDING_NEW, order_id="", ts=900.0)
    r2, _h2 = _rec(b2, [], fill_grace_s=30.0)
    rep = r2.run_once()
    assert rep["halt"] and rep["unexplained"][0]["kind"] == "lost_order"


def test_position_divergence_beyond_tolerance_halts():
    b = OrderBook()
    r = RC.Reconciler(b, fetch_orders=lambda: [], fetch_positions=lambda: {"M": (12.0, 0.0)},
                      local_positions=lambda: {"M": (10.0, 0.0)}, position_tolerance=1.0, clock=lambda: 1.0,
                      on_halt=lambda *a: None)
    rep = r.run_once()
    assert rep["halt"] and rep["unexplained"][0]["kind"] == "position_mismatch"
    r.position_tolerance = 5.0
    assert r.run_once()["halt"] is False


def test_the_scheduler_runs_on_its_interval_and_a_venue_error_is_a_halt_not_a_crash():
    b = OrderBook()
    t = [0.0]
    calls = []
    r = RC.Reconciler(b, fetch_orders=lambda: calls.append(1) or [], clock=lambda: t[0], on_halt=lambda *a: None)
    assert r.maybe_run(every_s=60) is not None and r.maybe_run(every_s=60) is None
    t[0] = 61.0
    assert r.maybe_run(every_s=60) is not None and len(calls) == 2

    def boom():
        raise OSError("venue down")

    r2 = RC.Reconciler(b, fetch_orders=boom, clock=lambda: 1.0, on_halt=lambda *a: None)
    rep = r2.run_once()
    assert rep["halt"] and rep["error"].startswith("OSError")


class ReconcileMachine(RuleBasedStateMachine):
    """Random place / fill / silent-cancel / stranger sequences: after every reconcile the
    local live set equals the venue's, and a halt happens exactly when a divergence has no
    recorded explanation (a silently vanished KNOWN order, or a venue order we never knew)."""

    def __init__(self):
        super().__init__()
        self.book = OrderBook()
        self.venue: dict = {}
        self.fills: list = []
        self.known: set = set()          # order ids the local book knows (placed or adopted)
        self.silent: set = set()         # known orders that vanished with no fill record
        self.strangers: set = set()      # venue orders the local book has never seen
        self.n = 0
        self.now = 1000.0

    def _next(self):
        self.n += 1
        return f"c{self.n}", f"V{self.n}"

    @rule()
    def place(self):
        coid, oid = self._next()
        _local(self.book, coid, order_id=oid)
        self.venue[oid] = _view(oid, coid)
        self.known.add(oid)

    @rule(data=st.data())
    def venue_fills_fully(self, data):
        if not self.venue:
            return
        oid = data.draw(st.sampled_from(sorted(self.venue)))
        v = self.venue.pop(oid)
        self.fills.append({"order_id": oid, "count": v.remaining, "ts": self.now - 1})
        self.strangers.discard(oid)

    @rule(data=st.data())
    def venue_cancels_silently(self, data):
        if not self.venue:
            return
        oid = data.draw(st.sampled_from(sorted(self.venue)))
        self.venue.pop(oid)
        if oid in self.known:
            self.silent.add(oid)
        self.strangers.discard(oid)

    @rule()
    def stranger_appears(self):
        _coid, oid = self._next()
        v = _view(oid, f"stranger-{self.n}", remaining=5.0)
        self.venue[oid] = v
        self.strangers.add(oid)

    @rule()
    def reconcile(self):
        # a known order that filled AND was silently cancelled is impossible here: sets are disjoint
        expected = len(self.silent - {f["order_id"] for f in self.fills}) + len(self.strangers)
        r = RC.Reconciler(self.book, fetch_orders=lambda: list(self.venue.values()),
                          fetch_fills=lambda: list(self.fills), clock=lambda: self.now, on_halt=lambda *a: None)
        rep = r.run_once()
        assert rep["halt"] == (expected > 0)
        # venue wins: afterwards the live local orders are exactly the venue's
        assert {o.order_id for o in self.book.resting()} == set(self.venue)
        self.known |= self.strangers
        self.silent.clear()
        self.strangers.clear()
        self.fills.clear()
        self.now += 1.0


TestReconcileMachine = ReconcileMachine.TestCase
TestReconcileMachine.settings = settings(max_examples=60, stateful_step_count=25, deadline=None)


# ============================================================ dead man's switch
def test_deadman_is_inert_without_a_url_and_never_exposes_it():
    d = D.DeadMansSwitch(url=None, fetch=lambda u, t: (_ for _ in ()).throw(AssertionError("must not call")))
    assert d.ping(now=100.0)["sent"] is False and d.status()["configured"] is False
    secret = "https://hc-ping.example/uuid-SECRET"
    calls = []
    d2 = D.DeadMansSwitch(url=secret, interval_s=60, fetch=lambda u, t: calls.append(u))
    assert d2.ping(now=100.0)["sent"] is True and calls == [secret]
    assert d2.ping(now=130.0)["sent"] is False                     # not due
    assert d2.ping(now=161.0)["sent"] is True
    assert secret not in json.dumps(d2.status()) and "SECRET" not in json.dumps(d2.status())


def test_deadman_does_not_ping_an_unhealthy_engine_and_failures_are_swallowed():
    calls = []
    d = D.DeadMansSwitch(url="https://x/y", interval_s=10, fetch=lambda u, t: calls.append(1))
    assert d.ping(now=100.0, healthy=False)["sent"] is False and calls == []
    assert d.status()["skipped_unhealthy"] == 1

    def boom(u, t):
        raise OSError("https://x/y refused")

    d2 = D.DeadMansSwitch(url="https://x/y", interval_s=10, fetch=boom)
    out = d2.ping(now=100.0)
    assert out["sent"] is False and out["error"] == "OSError"
    assert d2.status()["consecutive_failures"] == 1 and "x/y" not in json.dumps(d2.status())
    assert d2.ping(now=105.0)["sent"] is False                      # failed pings still honour the interval


def test_engine_reports_health_for_the_deadman(monkeypatch):
    from tests.test_review_loop_pnl import M, T0, newloop, program, snap
    lp = newloop(bankroll=1500.0)
    lp.on_frame(program(M))
    lp.on_frame(snap(M, T0, [(40, 2000)], [(55, 2000)]))
    monkeypatch.setattr("time.time", lambda: T0 + 5)
    ok, why = lp.healthy_for_deadman()
    assert ok and why == ""
    monkeypatch.setattr("time.time", lambda: T0 + 10_000)               # feed silent for hours
    assert lp.healthy_for_deadman() == (False, "feed_stale")
    monkeypatch.setattr("time.time", lambda: T0 + 5)
    lp.kill = {"reason": "x"}
    assert lp.healthy_for_deadman() == (False, "kill_latched")


def test_timer_pings_the_deadman_only_when_configured(monkeypatch, tmp_path):
    from mm.unattended.service import EngineTimer
    from tests.test_review_loop_pnl import newloop
    lp = newloop(bankroll=1500.0)
    calls = []
    lp.deadman = D.DeadMansSwitch(url="https://x/y", interval_s=1, fetch=lambda u, t: calls.append(1))
    EngineTimer(lp, heartbeat=str(tmp_path / "hb"), kill_path=str(tmp_path / "KILL")).tick()
    assert len(calls) == 1
    assert "deadman" in lp.live_snapshot() and "x/y" not in json.dumps(lp.live_snapshot()["deadman"])


# ============================================================ deploy verification
def _status(**kw):
    base = {"paper": True, "mode": "paper", "live_armed": False, "kill": None, "last_frame_ts": 1000.0,
            "build": {"commit": "abc1234def"}, "state": {"path": "/s", "error": None},
            "feed": {"connected": True}, "budget_warning": None}
    base.update(kw)
    return base


def test_verify_deploy_passes_a_healthy_paper_engine_on_the_expected_commit():
    from deploy import verify_deploy as V
    assert V.check(_status(), expect_commit="abc1234", now=1010.0) == []


@pytest.mark.parametrize("patch,needle", [
    ({"paper": False}, "paper"), ({"live_armed": True}, "live_armed"), ({"kill": {"reason": "x"}}, "kill"),
    ({"state": {"path": "/s", "error": "bad"}}, "state"), ({"feed": {"connected": False}}, "feed"),
    ({"build": {"commit": "zzz"}}, "commit"), ({"last_frame_ts": 1.0}, "frame"), ({"budget_warning": "x"}, "budget")])
def test_verify_deploy_names_every_failure(patch, needle):
    from deploy import verify_deploy as V
    fails = V.check(_status(**patch), expect_commit="abc1234", now=1010.0)
    assert fails and any(needle in f for f in fails)


def test_verify_deploy_fails_on_an_unreadable_status():
    from deploy import verify_deploy as V
    assert V.check({}, expect_commit=None, now=1.0)


def test_deploy_script_is_valid_bash_dry_runs_and_never_touches_arming_flags():
    script = ROOT / "deploy" / "deploy_and_verify.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    out = subprocess.run(["bash", str(script), "--dry-run"], capture_output=True, text=True,
                         env=dict(os.environ, LIP_RESTART_CMD="echo RESTART")).stdout
    assert "verify_deploy.py" in out and "DRY RUN" in out
    text = script.read_text()
    for forbidden in ("LIP_PAPER", "LIVE_ARMED", "LIVE_ACK", "ENFORCEMENT_VERIFIED"):
        assert forbidden not in text
