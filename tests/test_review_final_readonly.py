"""Final review F7: the read-only socket never ends a whole orderbook
subscription. Kalshi merges later subscribe batches into the same sid
(loop.SidSequencer), so a sid whose *tracked* tickers are all removed can
still carry tickers from a merged batch; ``unsubscribe`` would silently stop
them. Only update_subscription/delete_markets for the named tickers is sent."""
import asyncio
import json

from mm.unattended import loop as L
from mm.venues.readonly import TICKER_WS_CHANNELS
from tests.test_review_integ_readonly import _sock


class FakeKalshi:
    """Server side of the subscriptions: sid -> tickers, merging later
    subscribe batches for the same channel into the existing sid."""

    def __init__(self):
        self.sids = {}
        self.by_channel = {}
        self.next_sid = 11

    def apply(self, cmd):
        replies = []
        p = cmd.get("params") or {}
        if cmd["cmd"] == "subscribe":
            for ch in p["channels"]:
                sid = self.by_channel.get(ch)
                if sid is None:
                    sid = self.by_channel[ch] = self.next_sid
                    self.next_sid += 1
                    self.sids[sid] = set()
                    replies.append({"id": cmd["id"], "type": "subscribed", "msg": {"channel": ch, "sid": sid}})
                else:  # merged into the existing subscription; no new sid
                    replies.append({"id": cmd["id"], "type": "ok", "sid": sid, "seq": 1, "msg": {}})
                self.sids[sid] |= set(p.get("market_tickers") or [])
        elif cmd["cmd"] == "update_subscription" and p.get("action") == "delete_markets":
            for sid in p["sids"]:
                self.sids.get(sid, set()).difference_update(p["market_tickers"])
        elif cmd["cmd"] == "unsubscribe":
            for sid in p["sids"]:
                self.sids.pop(sid, None)
                self.by_channel = {c: s for c, s in self.by_channel.items() if s != sid}
        return replies

    def streams(self, ticker, channel="orderbook_delta"):
        sid = self.by_channel.get(channel)
        return sid is not None and ticker in self.sids.get(sid, set())


def _drive(sock, server):
    out = []
    for raw in list(sock._ws.sent):
        out.extend(server.apply(raw))
    sock._ws.sent.clear()
    for reply in out:
        sock.note_response(reply)


def test_tickers_from_a_merged_batch_keep_streaming():
    sock, server = _sock(), FakeKalshi()
    asyncio.run(L._subscribe(sock, ["A", "B"]))
    _drive(sock, server)
    asyncio.run(L._subscribe(sock, ["C", "D"]))  # later batch: merged into the same sids
    _drive(sock, server)
    assert all(server.streams(t) for t in "ABCD")

    sent = asyncio.run(sock.unsubscribe_markets(["A", "B"]))
    assert sent and all(c["cmd"] == "update_subscription" and c["params"]["action"] == "delete_markets"
                        for c in sent)
    assert all(sorted(c["params"]["market_tickers"]) == ["A", "B"] for c in sent)
    _drive_sent = list(sock._ws.sent)
    _drive(sock, server)
    assert "unsubscribe" not in [c["cmd"] for c in _drive_sent]
    assert not server.streams("A") and not server.streams("B")
    assert server.streams("C") and server.streams("D")

    # C's deltas on the shared sid still reach the loop
    got = []
    seqr = L.SidSequencer()
    sid = server.by_channel["orderbook_delta"]
    L._dispatch_ws_message({"type": "orderbook_delta", "sid": sid, "seq": 5, "ts": 1.0,
                            "msg": {"market_ticker": "C", "price_dollars": "0.40", "delta_fp": "1.00",
                                    "side": "yes"}}, got.append, seqr, sock)
    assert got and got[0]["msg"]["market_ticker"] == "C"


def test_ok_reply_with_the_full_ticker_list_is_tracked():
    sock = _sock()
    asyncio.run(sock.subscribe(sorted(TICKER_WS_CHANNELS), ["A"]))
    cid = sock._ws.sent[-1]["id"]
    sock.note_response({"id": cid, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 11}})
    sock.note_response({"id": 99, "type": "ok", "sid": 11, "seq": 3,
                        "msg": {"market_tickers": ["A", "C"]}})
    assert sock.sids[11]["tickers"] == {"A", "C"}


def test_removing_the_last_tracked_ticker_never_unsubscribes_the_sid():
    sock = _sock()
    asyncio.run(sock.subscribe(sorted(TICKER_WS_CHANNELS), ["A"]))
    cid = sock._ws.sent[-1]["id"]
    sock.note_response({"id": cid, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 11}})
    sock._ws.sent.clear()
    asyncio.run(sock.unsubscribe_markets(["A"]))
    assert [c["cmd"] for c in sock._ws.sent] == ["update_subscription"]
    assert sock._ws.sent[0]["params"] == {"sids": [11], "market_tickers": ["A"], "action": "delete_markets"}
    assert json.dumps(sock._ws.sent).count("unsubscribe") == 0
