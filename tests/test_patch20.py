"""Patch 20: Polymarket US paper maker venue; order endpoints hard-disabled."""
import pytest

from mm.unattended import pmus_paper as P


def test_order_and_write_requests_are_refused():
    for method, path in (("POST", "/v1/orders"), ("POST", "/v1/incentives"),
                         ("DELETE", "/v1/order/1"), ("GET", "/v1/orders/open"),
                         ("GET", "/v1/order/abc/modify"), ("GET", "/v1/portfolio/positions")):
        with pytest.raises(P.PMUSOrderBlocked):
            P.check_request(method, path)
    P.check_request("GET", "/v1/incentives?page_size=100&statuses=active")
    P.check_request("GET", "/v1/markets/rtc-x-2026-10-01-abc/book")


REC = [
    {"marketSlug": "ev-a-2026-10-01-x", "instrumentState": "INSTRUMENT_STATE_OPEN", "category": "CUL",
     "timePeriods": [{"programId": "p1", "programType": "liquidityProgram", "status": "active",
                      "rewardPool": 1000, "discountFactor": 0.25, "targetSize": 200,
                      "period": "daily_event"}]},
    {"marketSlug": "ev-a-2026-10-01-y", "instrumentState": "INSTRUMENT_STATE_OPEN",
     "timePeriods": [{"programId": "p1", "programType": "liquidityProgram", "status": "active",
                      "rewardPool": 1000, "discountFactor": 0.25, "targetSize": 200,
                      "period": "daily_event"}]},
    {"marketSlug": "nba-live", "instrumentState": "INSTRUMENT_STATE_OPEN",
     "timePeriods": [{"programId": "p2", "programType": "liquidityProgram", "status": "active",
                      "rewardPool": 5000, "discountFactor": 0.5, "targetSize": 100, "period": "live"}]},
]


def test_pool_split_and_period_filter():
    rows = P.programs_from_incentives(REC, now=0, periods={"daily_event"})
    assert [r["slug"] for r in rows] == ["ev-a-2026-10-01-x", "ev-a-2026-10-01-y"]
    assert rows[0]["pool_eff_usd"] == 500 and abs(rows[0]["rate_per_s"] - 500 / 86400) < 1e-12


def _book(bid=0.40, ask=0.45, qty=100):
    return {"marketData": {"bids": [{"px": {"value": f"{bid:.2f}"}, "qty": str(qty)}],
                           "offers": [{"px": {"value": f"{ask:.2f}"}, "qty": str(qty)}]}}


def test_evaluate_joins_touch_and_never_crosses():
    prog = P.programs_from_incentives(REC, now=0, periods={"daily_event"})[0]
    ev = P.evaluate(prog, P.parse_book(_book()), 100)
    assert ev["bid_px"] == 0.40 and ev["ask_px"] == 0.45 and ev["bid_px"] < ev["ask_px"]
    assert abs(ev["share"] - 0.5) < 1e-9  # we are half of each side
    assert abs(ev["capital"] - (40 + 55)) < 1e-9
    assert P.evaluate(prog, P.parse_book(_book(0.45, 0.45)), 100) is None  # locked book


class _Loop:
    class risk:
        class limits:
            gross_usd = 2000
    alloc_budget_usd = 1425.0
    committed = {"K": 1400}


def test_venue_respects_unified_cap_fills_and_inventory(monkeypatch):
    monkeypatch.setenv("LIP_PMUS_BUDGET_USD", "1000")
    monkeypatch.setenv("LIP_PMUS_MARKET_CAP_USD", "200")
    monkeypatch.setenv("LIP_PMUS_SIZES", "100")
    monkeypatch.setenv("LIP_PMUS_PERIODS", "daily_event")
    monkeypatch.setenv("LIP_MARKET_INV_CAP_USD", "25")
    books = {"ev-a-2026-10-01-x": _book(), "ev-a-2026-10-01-y": _book(0.30, 0.35)}
    calls = []

    def fetch(path):
        calls.append(path)
        P.check_request("GET", path)
        if path.startswith("/v1/incentives"):
            return {"programs": REC}
        return books[path.split("/")[3]]

    clock = {"t": 1000.0}
    v = P.PMUSPaperVenue(kalshi_loop=_Loop(), fetch=fetch, clock=lambda: clock["t"])
    v._stop.wait = lambda s: False
    assert abs(v.headroom_usd() - (1900 - 1425)) < 1e-6
    v.refresh_programs()
    v.select()
    assert len(v.quotes) == 2
    assert sum(q["capital"] for q in v.quotes.values()) <= v.headroom_usd() + 1e-9
    clock["t"] += 60
    books["ev-a-2026-10-01-x"] = _book(0.38, 0.40)  # offer trades down through our 40c bid
    v.poll_once()
    assert len(v.fills) == 1 and v.fills[0]["side"] == "yes" and v.rebate_usd > 0
    assert v.reward_usd > 0
    q = v.quotes["ev-a-2026-10-01-x"]
    assert q["sides"] == ("no",)  # $40 unpaired > $25 cap: adding side blocked
    s = v.summary()
    assert s["paper"] and s["fills_n"] == 1 and "hard-disabled" in s["order_endpoints"]
    assert all(not c.startswith("/v1/order") for c in calls)


def test_period_windows_and_tick():
    ev = P._ts("2026-10-10 19:30:00+00")
    assert ev == P._ts("2026-10-10T19:30:00Z")
    assert not P.period_open("live", ev - 10, None, None, ev)
    assert P.period_open("live", ev + 10, None, None, ev)
    assert P.period_open("day_of", ev - 3600, None, None, ev)
    assert not P.period_open("day_of", ev - 7 * 3600, None, None, ev)
    assert P.period_open("early", ev - 7 * 3600, None, None, ev)
    assert not P.period_open("daily_event", 100, 200, None, ev)
    rec = [{"marketSlug": "g", "eventStartTime": "2026-10-10 19:30:00+00",
            "timePeriods": [{"programId": "x", "programType": "liquidityProgram", "status": "active",
                             "rewardPool": 10, "discountFactor": .1, "targetSize": 5, "period": "live"}]}]
    assert P.programs_from_incentives(rec, now=ev - 86400, periods=set()) == []
    assert len(P.programs_from_incentives(rec, now=ev + 60, periods=set())) == 1
    assert P.infer_tick({"bids": [(0.775, 1)], "offers": [(0.81, 1)]}) == 0.005
    assert P.infer_tick({"bids": [(0.77, 1)], "offers": [(0.81, 1)]}) == 0.01
