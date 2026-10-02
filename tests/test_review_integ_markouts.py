"""Phase 4 instrumentation: multi-horizon paper fill markouts (measurement
only) against the book mark and, when available, the external fair value."""
import pytest

from mm.unattended.markouts import HORIZONS, MarkoutBook
from tests.test_review_integ_loop import (  # noqa: F401  (autouse env fixture)
    K, PM, T0, _env, _pm_print, newloop, pm_loop, program, snap, trade,
)


def _cell(rep, ref, horizon, **slice_):
    node = rep["by_ref"][ref][horizon]
    if not slice_:
        return node["all"]
    (kind, name), = slice_.items()
    return node[kind].get(name)


def test_horizons_are_the_playbook_set():
    assert [h for h, _s in HORIZONS] == ["5s", "30s", "2m", "10m", "1h"]
    assert [s for _h, s in HORIZONS] == [5.0, 30.0, 120.0, 600.0, 3600.0]


def test_markout_against_mid_and_missing_fair_value():
    book = MarkoutBook()
    book.add(market=K, side="yes", price_cents=40.0, count=100.0, ts=T0, venue="kalshi",
             bucket="short", mid0=41.0, synthetic=False)
    book.on_clock(T0 + 5, lambda m, sd: 43.0, lambda m, sd: None)
    rep = book.report()
    c = _cell(rep, "mid", "5s")
    assert c == {"n": 1, "contracts": 100.0, "mean_cents": pytest.approx(3.0), "usd": pytest.approx(3.0)}
    assert _cell(rep, "mid", "5s", venue="kalshi")["n"] == 1
    assert _cell(rep, "mid", "5s", bucket="short")["usd"] == pytest.approx(3.0)
    # no fair value for this market: recorded as missing, never substituted
    assert _cell(rep, "fv", "5s")["n"] == 0
    assert rep["missing"]["fv"]["5s"] == 1
    assert _cell(rep, "mid", "30s")["n"] == 0      # not due yet
    assert rep["pending_fills"] == 1


def test_no_side_markout_uses_the_no_mark_and_fair_value():
    book = MarkoutBook()
    book.add(market=K, side="no", price_cents=55.0, count=10.0, ts=T0, venue="kalshi",
             bucket="durable", mid0=57.5, synthetic=False)
    # caller supplies side marks: NO mark 52 (YES 48), NO fair value 60
    book.on_clock(T0 + 5, lambda m, sd: 52.0 if sd == "no" else 48.0,
                  lambda m, sd: 60.0 if sd == "no" else 40.0)
    rep = book.report()
    assert _cell(rep, "mid", "5s")["usd"] == pytest.approx(10 * (52 - 55) / 100.0)
    assert _cell(rep, "fv", "5s")["usd"] == pytest.approx(10 * (60 - 55) / 100.0)
    assert _cell(rep, "fv", "5s")["mean_cents"] == pytest.approx(5.0)


def test_late_checks_are_counted_not_averaged():
    book = MarkoutBook()
    book.add(market=K, side="yes", price_cents=40.0, count=100.0, ts=T0, venue="kalshi",
             bucket="short", mid0=41.0, synthetic=False)
    book.on_clock(T0 + 700, lambda m, sd: 45.0, lambda m, sd: None)  # e.g. after a feed gap
    rep = book.report()
    for h in ("5s", "30s", "2m"):
        assert _cell(rep, "mid", h)["n"] == 0 and rep["late"][h] == 1
    assert _cell(rep, "mid", "10m")["n"] == 1      # 100 s late on a 600 s horizon: on time
    assert _cell(rep, "mid", "1h")["n"] == 0 and rep["pending_fills"] == 1


def test_pending_memory_is_bounded():
    book = MarkoutBook(max_pending=10)
    for i in range(25):
        book.add(market=f"{K}{i}", side="yes", price_cents=40.0, count=1.0, ts=T0 + i,
                 venue="kalshi", bucket="short", mid0=41.0, synthetic=False)
    rep = book.report()
    assert rep["pending_fills"] == 10 and rep["dropped_fills"] == 15
    assert len(book._heap) <= 6 * 10 + 5 * 10
    book.on_clock(T0 + 10_000, lambda m, sd: 41.0, lambda m, sd: None)
    assert book.report()["pending_fills"] == 0 and len(book._heap) == 0


def test_settlement_markout_from_held_legs():
    book = MarkoutBook()
    book.on_settle(K, "no", [("kalshi", "short", 100.0, 0.0, 40.0, 0.0, 1)])
    c = book.report()["by_ref"]["settlement"]["settle"]
    assert c["all"]["usd"] == pytest.approx(-40.0) and c["all"]["mean_cents"] == pytest.approx(-40.0)
    assert c["venue"]["kalshi"]["n"] == 1 and c["bucket"]["short"]["contracts"] == 100.0


def test_aggregates_survive_a_state_round_trip():
    book = MarkoutBook()
    book.add(market=K, side="yes", price_cents=40.0, count=100.0, ts=T0, venue="kalshi",
             bucket="short", mid0=41.0, synthetic=False)
    book.on_clock(T0 + 5, lambda m, sd: 43.0, lambda m, sd: None)
    other = MarkoutBook()
    other.load_state(book.state())
    assert other.report()["by_ref"] == book.report()["by_ref"]
    assert other.spread_usd == pytest.approx(1.0)
    assert other.report()["pending_fills"] == 0     # pending checks are not persisted


# ------------------------------------------------------------------ RunLoop wiring
def _kalshi_filled():
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(trade(K, T0 + 2, "t1", 30, 5000, "no"))
    assert [(f["side"], f["price_cents"], f["count"]) for f in lp.fills] == [("yes", 40, 100.0)]
    return lp


def test_loop_records_horizon_markouts_on_its_clock():
    lp = _kalshi_filled()
    lp.on_frame(snap(K, T0 + 3, [(10, 500)], [(85, 500)]))      # mid 12.5
    lp.on_frame({"type": "clock", "ts": T0 + 7})
    rep = lp.live_snapshot()["markout_horizons"]
    assert "estimate (paper)" in rep["label"]
    c = rep["by_ref"]["mid"]["5s"]
    assert c["all"]["n"] == 1 and c["all"]["usd"] == pytest.approx(100 * (12.5 - 40) / 100.0)
    assert c["venue"]["kalshi"]["n"] == 1 and c["bucket"][lp.bucket_of[K]]["n"] == 1
    assert rep["by_ref"]["fv"]["5s"]["all"]["n"] == 0 and rep["missing"]["fv"]["5s"] == 1
    from mm.status_page import status_payload
    assert status_payload(lp.live_snapshot())["markout_horizons"]["by_ref"]["mid"]["5s"]["all"]["n"] == 1


def test_loop_measures_against_fair_value_when_present():
    lp = _kalshi_filled()

    class FV:
        def get(self, market, now=None):
            return {"fv_cents": 30.0, "ts": now} if market == K else None

        def summary(self):
            return {}

    lp.fv = FV()
    lp.on_frame({"type": "clock", "ts": T0 + 7})
    c = lp.live_snapshot()["markout_horizons"]["by_ref"]["fv"]["5s"]["all"]
    assert c["n"] == 1 and c["usd"] == pytest.approx(100 * (30 - 40) / 100.0)


def test_loop_settlement_markout_and_synthetic_slice():
    lp = _kalshi_filled()
    lp.settle(K, "yes")
    rep = lp.live_snapshot()["markout_horizons"]
    assert rep["by_ref"]["settlement"]["settle"]["all"]["usd"] == pytest.approx(60.0)
    pm = pm_loop()
    pm.on_frame(_pm_print(T0 + 2, "pmus:abc:1", 40, 50, "no"))
    pm.on_frame({"type": "clock", "ts": T0 + 8})
    r2 = pm.live_snapshot()["markout_horizons"]
    assert r2["by_ref"]["mid"]["5s"]["venue"]["pmus"]["n"] == 1
    assert r2["synthetic_fills"] == 1


def test_loop_state_file_keeps_markout_aggregates(tmp_path):
    lp = _kalshi_filled()
    lp.on_frame({"type": "clock", "ts": T0 + 7})
    path = tmp_path / "state.json"
    lp.state_path = str(path)
    assert lp.save_state(force=True)
    again = newloop()
    again.attach_state(str(path))
    assert again.kill is None
    assert (again.live_snapshot()["markout_horizons"]["by_ref"]["mid"]["5s"]
            == lp.live_snapshot()["markout_horizons"]["by_ref"]["mid"]["5s"])
