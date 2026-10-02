"""Integration: market_lifecycle_v2 settlements reach the loop over the
read-only socket, and pruned tickers are removed from the live subscription."""
import asyncio
import json

import pytest

from mm.unattended import loop as L
from mm.venues.readonly import (
    LIFECYCLE_WS_CHANNEL, PROD_WS, PUBLIC_WS_CHANNELS, TICKER_WS_CHANNELS,
    ReadOnlyMarketSocket, ReadOnlyViolation,
)
from tests.test_review_integ_loop import K, T0, _env, newloop, program, snap, trade  # noqa: F401


class _Key:
    def sign(self, data, pad, alg):
        return b"sig"


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _sock():
    sock = ReadOnlyMarketSocket(api_key="read-key", private_key=_Key(), url=PROD_WS)
    sock._ws = _WS()
    return sock


def test_lifecycle_channel_is_allowed_and_subscribed_without_tickers():
    assert LIFECYCLE_WS_CHANNEL == "market_lifecycle_v2"
    assert LIFECYCLE_WS_CHANNEL in PUBLIC_WS_CHANNELS
    assert LIFECYCLE_WS_CHANNEL not in TICKER_WS_CHANNELS
    cmd = _sock().command([LIFECYCLE_WS_CHANNEL])
    assert cmd["cmd"] == "subscribe"
    # the channel takes no market filter (docs.kalshi.com websockets)
    assert cmd["params"] == {"channels": ["market_lifecycle_v2"]}
    with pytest.raises(ReadOnlyViolation):
        _sock().command([LIFECYCLE_WS_CHANNEL, "trade"], ["X"])
    with pytest.raises(ReadOnlyViolation):
        _sock().command([LIFECYCLE_WS_CHANNEL], ["X"])
    for private in ("fill", "market_positions", "user_orders", "order_group_updates", "communications"):
        with pytest.raises(ReadOnlyViolation):
            _sock().command([private], ["X"])


def test_session_subscribes_lifecycle_once():
    sock = _sock()
    asyncio.run(L._subscribe_lifecycle(sock))
    assert [c["params"]["channels"] for c in sock._ws.sent] == [["market_lifecycle_v2"]]


def test_ticker_subscribe_uses_only_ticker_channels():
    sent = []

    class Sock:
        async def subscribe(self, ch, tickers):
            sent.append((list(ch), list(tickers)))

    asyncio.run(L._subscribe(Sock(), ["B", "A"]))
    assert sent == [(sorted(TICKER_WS_CHANNELS), ["A", "B"])]


def _subscribed(sock):
    asyncio.run(sock.subscribe(sorted(TICKER_WS_CHANNELS), ["A", "B"]))
    cid = sock._ws.sent[-1]["id"]
    for sid, ch in ((11, "orderbook_delta"), (12, "ticker"), (13, "trade")):
        sock.note_response({"id": cid, "type": "subscribed", "msg": {"channel": ch, "sid": sid}})
    sock._ws.sent.clear()


def test_unsubscribe_markets_deletes_from_each_subscription_never_unsubscribes():
    sock = _sock()
    _subscribed(sock)
    asyncio.run(sock.unsubscribe_markets(["A", "ZZZ"]))
    cmds = sock._ws.sent
    assert sorted(c["params"]["sids"][0] for c in cmds) == [11, 12, 13]
    for c in cmds:
        assert c["cmd"] == "update_subscription"
        assert c["params"]["action"] == "delete_markets"
        assert c["params"]["market_tickers"] == ["A"]
    sock._ws.sent.clear()
    # the last tracked ticker of a subscription: still delete_markets for
    # that ticker only (a merged batch may share the sid), never unsubscribe
    asyncio.run(sock.unsubscribe_markets(["B"]))
    assert [c["cmd"] for c in sock._ws.sent] == ["update_subscription"] * 3
    assert sorted(c["params"]["sids"][0] for c in sock._ws.sent) == [11, 12, 13]
    assert all(c["params"]["market_tickers"] == ["B"] for c in sock._ws.sent)
    sock._ws.sent.clear()
    asyncio.run(sock.unsubscribe_markets(["B"]))   # nothing left: nothing sent
    assert sock._ws.sent == []


def test_unsubscribe_never_touches_the_lifecycle_subscription():
    sock = _sock()
    asyncio.run(sock.subscribe([LIFECYCLE_WS_CHANNEL]))
    cid = sock._ws.sent[-1]["id"]
    sock.note_response({"id": cid, "type": "subscribed",
                        "msg": {"channel": LIFECYCLE_WS_CHANNEL, "sid": 5}})
    sock._ws.sent.clear()
    asyncio.run(sock.unsubscribe_markets(["A"]))
    assert sock._ws.sent == []


def test_pruned_programs_are_unsubscribed_by_the_background_refresh():
    lp = newloop()
    lp.on_frame(program(K, end_ts=T0 - 7200))
    ctx = {"fed": {K: 1}, "sigs": {K: ()}, "ends": {K: T0 - 7200}}
    gone = L._prune_fed([], ctx, lp.on_frame, now=T0)
    assert gone == [K] and ctx["unsubscribe_pending"] == [K]
    calls = []

    class Sock:
        async def unsubscribe_markets(self, tickers):
            calls.append(list(tickers))

    asyncio.run(L._flush_unsubscribes(Sock(), ctx))
    assert calls == [[K]] and ctx["unsubscribe_pending"] == []


def _held_loop():
    lp = newloop()
    lp.on_frame(program(K))
    lp.on_frame(snap(K, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    lp.on_frame({"type": "clock", "ts": T0 + 1})
    lp.on_frame(trade(K, T0 + 2, "t1", 30, 5000, "no"))
    assert lp.position[K]["yes"] == 100
    return lp


def test_lifecycle_determined_message_settles_held_inventory():
    lp = _held_loop()
    seqr = L.SidSequencer()
    msg = {"type": "market_lifecycle_v2", "sid": 5, "seq": 1, "ts": T0 + 10,
           "msg": {"market_ticker": K, "event_type": "determined", "result": "yes"}}
    L._dispatch_ws_message(msg, lp.on_frame, seqr, None)
    assert lp.settled[K]["result"] == "yes"
    assert lp.pnl_parts()["markout_usd"] == pytest.approx(60.0)   # 100 YES @40c settle at 100c


def test_lifecycle_for_markets_we_never_touched_is_not_stored():
    lp = _held_loop()
    seqr = L.SidSequencer()
    for i in range(50):
        L._dispatch_ws_message({"type": "market_lifecycle_v2", "ts": T0 + 10, "msg": {
            "market_ticker": f"KXOTHER-{i}", "event_type": "settled", "result": "no"}},
            lp.on_frame, seqr, None)
    assert lp.settled == {} and not any(m.startswith("KXOTHER") for m in lp.last_mid)


def test_update_subscription_ack_advances_the_sid_sequence():
    """The 'ok' ack of update_subscription carries the subscription's seq;
    it must not make the next book message look like a gap."""
    lp = newloop()
    seqr = L.SidSequencer()
    frames = []
    L._dispatch_ws_message({"type": "orderbook_snapshot", "sid": 11, "seq": 1, "ts": T0,
                            "msg": {"market_ticker": K, "yes_dollars_fp": [], "no_dollars_fp": []}},
                           frames.append, seqr, None)
    L._dispatch_ws_message({"type": "ok", "sid": 11, "seq": 2, "id": 7,
                            "msg": {"market_tickers": ["B"]}}, frames.append, seqr, None)
    L._dispatch_ws_message({"type": "orderbook_delta", "sid": 11, "seq": 3, "ts": T0 + 1,
                            "msg": {"market_ticker": K, "side": "yes", "price_dollars": "0.4000",
                                    "delta_fp": "1"}}, frames.append, seqr, None)
    assert [f["type"] for f in frames] == ["orderbook_snapshot", "orderbook_delta"]
