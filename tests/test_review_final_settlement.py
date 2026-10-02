"""Final review F2: positions that never see a websocket settlement must not
lock capital forever.

(a) Kalshi: held positions past close (or with no program left) are
    backfilled from read-only GET /markets/{ticker} on the background refresh
    path and booked through RunLoop.settle.
(b) PM US: the public gateway's GET /v1/markets/{slug}/settlement is polled
    for held PM US positions past close; with no usable settlement, after
    close + LIP_PMUS_UNSETTLED_RELEASE_S (24 h) the capital is released from
    budgets/caps while P&L keeps it at a full loss and status lists it under
    ``unresolved_positions``.
(c) Any position still unsettled 24 h past close raises an alert."""
import asyncio
import json
import urllib.error

import pytest

from mm.status_page import status_payload
from mm.unattended import loop as L
from mm.unattended import pmus_paper as P
from mm.venues.readonly import ReadOnlyKalshiTransport, ReadOnlyViolation, get_allowed
from tests.test_review_integ_loop import K, PM, T0, _env, newloop, pm_program, program  # noqa: F401

DAY = 86400.0
CLOSE = T0 + 3600


def _held(market, prog, side="yes", count=100.0, price=40.0):
    lp = newloop(bankroll=1500.0)
    lp.on_frame(prog)
    lp.on_frame({"type": "clock", "ts": T0})
    lp._record_fill({"market_ticker": market, "side": side, "price_cents": price, "count": count,
                     "trade_id": "f1"}, T0 + 1)
    assert lp.position[market][side] == count
    assert float(lp.inv_committed[market]) == pytest.approx(count * price / 100)
    return lp


def _kalshi():
    return _held(K, program(K, end_ts=T0 + 1800, close_ts=CLOSE))


def _pmus():
    return _held(PM, pm_program(end_ts=T0 + 1800, close_ts=CLOSE))


def _clock(lp, ts):
    lp.on_frame({"type": "clock", "ts": ts})


# ------------------------------------------------------------------ (a) Kalshi
class _Reader:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(path)
        assert get_allowed(path)
        return {"market": self.rows[path.rsplit("/", 1)[1]]}


def test_kalshi_held_position_past_close_is_settled_from_rest():
    lp = _kalshi()
    _clock(lp, T0 + 600)
    assert K not in [m for m, _v in lp.settle_view]  # still open: nothing to ask
    lp.prune_ended(CLOSE + 7 * 3600)  # the program is gone; the position stays
    _clock(lp, CLOSE + 7 * 3600)
    assert (K, "kalshi") in lp.settle_view
    reader = _Reader({K: {"ticker": K, "status": "finalized", "result": "no"}})
    ctx = {"settle_candidates": lambda: [m for m, v in lp.settle_view if v == "kalshi"]}
    n = asyncio.run(L._settlement_backfill(reader, ctx, lp.on_frame))
    assert n == 1 and reader.calls == [f"/markets/{K}"]
    assert lp.settled[K]["result"] == "no" and lp.settled[K]["source"] == "rest_backfill"
    assert float(lp.risk.market_usd.get(K, 0)) == 0 and float(lp.inv_committed[K]) == 0
    assert lp.locked_usd().get("kalshi", 0.0) == 0.0
    assert lp.pnl_parts()["markout_usd"] == pytest.approx(-40.0)
    assert K not in lp.positions_report()


@pytest.mark.parametrize("row", [
    {"status": "closed", "result": ""},
    {"status": "disputed", "result": "yes"},
    {"status": "determined", "result": "scalar"},
])
def test_kalshi_backfill_books_only_a_final_binary_result(row):
    lp = _kalshi()
    lp.prune_ended(CLOSE + 7 * 3600)
    _clock(lp, CLOSE + 7 * 3600)
    reader = _Reader({K: dict(row, ticker=K)})
    ctx = {"settle_candidates": lambda: [K]}
    assert asyncio.run(L._settlement_backfill(reader, ctx, lp.on_frame)) == 0
    assert K not in lp.settled


def test_kalshi_backfill_throttles_per_ticker_and_uses_the_get_only_reader():
    sent = []

    class Sess:
        def request(self, method, url, **kw):
            sent.append((method, url))

            class R:
                status_code = 200

                def json(self):
                    return {"market": {"ticker": K, "status": "active", "result": ""}}
            return R()

    class Key:
        def sign(self, *a):
            return b"s"
    reader = ReadOnlyKalshiTransport(api_key="k", private_key=Key(), session=Sess())
    ctx = {"settle_candidates": lambda: [K]}
    asyncio.run(L._settlement_backfill(reader, ctx, lambda f: None, now=1000.0))
    asyncio.run(L._settlement_backfill(reader, ctx, lambda f: None, now=1100.0))  # within LIP_SETTLE_POLL_S
    assert sent == [("GET", f"https://api.elections.kalshi.com/trade-api/v2/markets/{K}")]
    with pytest.raises(ReadOnlyViolation):
        reader.request("POST", f"/markets/{K}")


def test_background_refresh_runs_the_backfill(monkeypatch):
    seen = []

    async def fake_backfill(reader, ctx, on_frame, now=None):
        seen.append(ctx["settle_candidates"]())
        raise asyncio.CancelledError
    monkeypatch.setattr(L, "_settlement_backfill", fake_backfill)
    monkeypatch.setattr(L, "_refresh_meta", lambda reader, ctx: None)
    monkeypatch.setattr(L, "_screen_and_feed", lambda ctx, on_frame: [])

    async def no_flush(sock, ctx):
        return None
    monkeypatch.setattr(L, "_flush_unsubscribes", no_flush)
    ctx = {"programs": [{"market": "X"}], "refresh_s": 600.0, "settle_candidates": lambda: [K]}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(L._background(object(), ctx, lambda f: None, None, {}))
    assert seen == [[K]]


# ------------------------------------------------------------------ (b) PM US
def _feed(lp, responses):
    calls = []

    def fetch(path):
        P.check_request("GET", path)
        calls.append(path)
        r = responses.get(path)
        if isinstance(r, Exception):
            raise r
        return r
    feed = P.PMUSFeed(lp, fetch=fetch, clock=lambda: lp.now, sleep=lambda s: None)
    return feed, calls


def test_pmus_settlement_endpoint_is_get_only_allowlisted():
    P.check_request("GET", "/v1/markets/abc-def-2026-12-01/settlement")
    for bad in (("POST", "/v1/markets/abc/settlement"), ("GET", "/v1/markets/../settlement"),
                ("GET", "/v1/markets/abc/settlement/x"), ("GET", "/v1/markets/abc/order/settlement")):
        with pytest.raises(P.PMUSOrderBlocked):
            P.check_request(*bad)


def test_pmus_position_past_close_is_settled_from_the_gateway():
    lp = _pmus()
    _clock(lp, CLOSE + 60)
    assert (PM, "pmus") in lp.settle_view
    slug = PM[len(P.PREFIX):]
    feed, calls = _feed(lp, {f"/v1/markets/{slug}/settlement": {"slug": slug, "settlement": "1"}})
    assert feed.poll_settlements() == 1
    lp.drain_external()
    assert calls == [f"/v1/markets/{slug}/settlement"]
    assert lp.settled[PM]["result"] == "yes" and lp.settled[PM]["source"] == "pmus_gateway"
    assert float(lp.inv_committed[PM]) == 0 and lp.locked_usd().get("pmus", 0.0) == 0.0
    assert lp.pnl_parts()["markout_usd"] == pytest.approx(60.0)


@pytest.mark.parametrize("resp", [
    urllib.error.HTTPError("u", 404, "not settled", {}, None),
    {"slug": "abc-def-2026-12-01", "settlement": "0.5"},
    {"slug": "other-slug", "settlement": "1"},
])
def test_pmus_unusable_settlement_is_not_booked(resp):
    lp = _pmus()
    _clock(lp, CLOSE + 60)
    slug = PM[len(P.PREFIX):]
    feed, _calls = _feed(lp, {f"/v1/markets/{slug}/settlement": resp})
    feed.poll_settlements()
    lp.drain_external()
    assert PM not in lp.settled


def test_pmus_unsettled_position_releases_capital_after_24h_but_keeps_the_loss(monkeypatch):
    import monitor.alerts
    sent = []
    monkeypatch.setattr(monitor.alerts, "alert", lambda *a, **k: sent.append(a))
    lp = _pmus()
    _clock(lp, CLOSE + DAY - 60)
    assert PM not in lp.unresolved and float(lp.inv_committed[PM]) == pytest.approx(40.0)
    _clock(lp, CLOSE + DAY + 1)
    assert PM in lp.unresolved
    assert float(lp.inv_committed[PM]) == 0 and float(lp.risk.market_usd.get(PM, 0)) == 0
    assert float(lp.risk.venue_usd.get("pmus", 0)) == 0 and lp.locked_usd().get("pmus", 0.0) == 0.0
    # P&L: worst case, the whole cost basis is lost
    assert lp.pnl_parts()["markout_usd"] == pytest.approx(-40.0)
    st = json.loads(json.dumps(status_payload(lp.live_snapshot(session_start_ts=T0))))
    assert PM in st["unresolved_positions"] and PM not in st["positions"]
    assert st["unresolved_positions"][PM]["cost_usd"] == pytest.approx(40.0)
    assert sum(b["markout_usd"] for b in st["buckets"].values()) == pytest.approx(-40.0)
    assert any("unsettled" in a[2] for a in sent)
    # a late settlement still books normally
    lp.on_frame({"kind": "settlement", "market": PM, "result": "yes", "ts": CLOSE + 2 * DAY})
    assert PM in lp.settled and PM not in lp.unresolved
    assert lp.pnl_parts()["markout_usd"] == pytest.approx(60.0)


def test_unresolved_and_close_survive_a_restart(tmp_path):
    lp = _pmus()
    path = str(tmp_path / "engine_state.json")
    lp.attach_state(path)
    lp.prune_ended(CLOSE + 7 * 3600)  # program gone; close remembered on the position
    _clock(lp, CLOSE + DAY + 1)
    assert PM in lp.unresolved
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(path)
    assert PM in lp2.unresolved and lp2.position[PM]["close_ts"] == pytest.approx(CLOSE)
    assert float(lp2.inv_committed.get(PM, 0)) == 0 and lp2.locked_usd().get("pmus", 0.0) == 0.0
    assert lp2.pnl_parts()["markout_usd"] == pytest.approx(-40.0)
    # a restored position whose program is gone and close passed is still asked about
    _clock(lp2, CLOSE + DAY + 5)
    assert (PM, "pmus") in lp2.settle_view


# ------------------------------------------------------------------ (c) alert
def test_kalshi_position_unsettled_24h_past_close_alerts_once(monkeypatch):
    import monitor.alerts
    sent = []
    monkeypatch.setattr(monitor.alerts, "alert", lambda *a, **k: sent.append(a))
    lp = _kalshi()
    lp.prune_ended(CLOSE + 7 * 3600)
    _clock(lp, CLOSE + DAY - 5)
    assert not any("unsettled" in a[2] for a in sent)
    _clock(lp, CLOSE + DAY + 1)
    _clock(lp, CLOSE + DAY + 2)
    hits = [a for a in sent if "unsettled" in a[2]]
    assert len(hits) == 1 and K in hits[0][2]
    # Kalshi capital is not released by the fallback (the REST backfill settles it)
    assert K not in lp.unresolved and float(lp.inv_committed[K]) == pytest.approx(40.0)
