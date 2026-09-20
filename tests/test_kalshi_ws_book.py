"""Book engine tests for execution/kalshi_ws.py (2026-09-20 audit #1).

Covers:
  - v2 snapshot format (yes_dollars_fp / no_dollars_fp, fixed-point strings)
  - v2 delta format (price_dollars / delta_fp) with legacy fallback
  - fractional sizes are preserved, not rounded
  - per-subscription seq tracking: any gap → stale, deltas dropped,
    snapshot clears
  - deltas from a superseded sid are ignored
  - replay of a captured official-format sequence reproduces the venue book
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import kalshi_ws as kw
from execution.kalshi_ws import BookLevel, BookState, KalshiWS


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))


class _TestWS(KalshiWS):
    """KalshiWS without key loading or a socket."""

    def _load_key(self):
        self._private_key = None


@pytest.fixture
def ws(monkeypatch):
    monkeypatch.setattr(kw.settings, "WS_SEQ_GAP_TOLERANCE", 0, raising=False)
    w = _TestWS(api_key="k", private_key_path="/nonexistent")
    w._ws = _FakeWS()
    w.books["MKT"] = BookState(market_ticker="MKT")
    return w


def _snap(ticker="MKT", sid=7, seq=1, yes=None, no=None):
    return json.dumps({
        "type": "orderbook_snapshot", "sid": sid, "seq": seq,
        "msg": {"market_ticker": ticker,
                "yes_dollars_fp": yes if yes is not None else [],
                "no_dollars_fp": no if no is not None else []},
    })


def _delta(ticker="MKT", sid=7, seq=2, side="yes", price="0.4900", delta="10.00"):
    return json.dumps({
        "type": "orderbook_delta", "sid": sid, "seq": seq,
        "msg": {"market_ticker": ticker, "side": side,
                "price_dollars": price, "delta_fp": delta},
    })


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _levels(side):
    return [(l.price_cents, l.size) for l in side]


# ── Parsing ───────────────────────────────────────────────────────────────

class TestParseSnapshot:
    def test_v2_fixed_point_lists(self):
        out = KalshiWS._parse_book_side([["0.4900", "501.00"], ["0.4800", "100.50"]])
        assert _levels(out) == [(49, 501.0), (48, 100.5)]

    def test_fractional_size_preserved(self):
        out = KalshiWS._parse_book_side([["0.0100", "0.25"]])
        assert out[0].size == pytest.approx(0.25)

    def test_v2_dict_keys(self):
        out = KalshiWS._parse_book_side([{"price_dollars": "0.3300", "size_fp": "12.50"}])
        assert _levels(out) == [(33, 12.5)]

    def test_legacy_dict_keys_still_work(self):
        out = KalshiWS._parse_book_side([{"price": 33, "size": 12}])
        assert _levels(out) == [(33, 12.0)]

    def test_zero_and_garbage_levels_dropped(self):
        out = KalshiWS._parse_book_side([
            ["0.4900", "0.00"], ["abc", "5"], ["0.4900", "nan"], ["1.5000", "5"],
            ["0.5000"], 42, None,
        ])
        assert out == []

    def test_empty_side_is_valid_not_missing(self, ws):
        # An explicit empty list must clear the side (not fall through to
        # a legacy key or keep the old levels).
        book = ws.books["MKT"]
        book.yes_bids = [BookLevel(50, 10.0)]
        ws._apply_snapshot(book, {"yes_dollars_fp": [], "no_dollars_fp": [["0.4000", "5"]]})
        assert book.yes_bids == []
        assert _levels(book.no_bids) == [(40, 5.0)]
        assert _levels(book.yes_asks) == [(60, 5.0)]


class TestApplyDelta:
    def test_v2_delta_keys_apply(self, ws):
        book = ws.books["MKT"]
        ws._apply_snapshot(book, {"yes_dollars_fp": [["0.4900", "100.00"]], "no_dollars_fp": []})
        assert ws._apply_delta(book, {"side": "yes", "price_dollars": "0.4900", "delta_fp": "-40.50"})
        assert _levels(book.yes_bids) == [(49, 59.5)]
        # implied NO ask re-derived
        assert _levels(book.no_asks) == [(51, 59.5)]

    def test_regression_legacy_keys_absent_used_to_zero_out(self, ws):
        """Before the fix, a v2 delta parsed to price=0/delta=0 and the book
        silently froze at the snapshot. Now the level actually moves."""
        book = ws.books["MKT"]
        ws._apply_snapshot(book, {"yes_dollars_fp": [["0.4900", "100.00"]], "no_dollars_fp": []})
        ws._apply_delta(book, {"side": "yes", "price_dollars": "0.4900", "delta_fp": "-100.00"})
        assert book.yes_bids == []
        assert book.no_asks == []

    def test_legacy_delta_keys_fallback(self, ws):
        book = ws.books["MKT"]
        ws._apply_delta(book, {"side": "no", "price": 30, "delta": 7})
        assert _levels(book.no_bids) == [(30, 7.0)]

    def test_new_level_inserted_sorted_desc(self, ws):
        book = ws.books["MKT"]
        ws._apply_delta(book, {"side": "yes", "price_dollars": "0.4000", "delta_fp": "1"})
        ws._apply_delta(book, {"side": "yes", "price_dollars": "0.4500", "delta_fp": "1"})
        ws._apply_delta(book, {"side": "yes", "price_dollars": "0.4200", "delta_fp": "1"})
        assert [l.price_cents for l in book.yes_bids] == [45, 42, 40]

    def test_negative_delta_on_missing_level_ignored(self, ws):
        book = ws.books["MKT"]
        assert ws._apply_delta(book, {"side": "yes", "price_dollars": "0.4000", "delta_fp": "-5"})
        assert book.yes_bids == []

    def test_bad_side_or_price_rejected(self, ws):
        book = ws.books["MKT"]
        assert not ws._apply_delta(book, {"side": "maybe", "price_dollars": "0.4", "delta_fp": "1"})
        assert not ws._apply_delta(book, {"side": "yes", "price_dollars": "x", "delta_fp": "1"})
        assert not ws._apply_delta(book, {"side": "yes", "price_dollars": "0.4", "delta_fp": None})
        assert book.delta_count == 0


# ── Sequence tracking ─────────────────────────────────────────────────────

class TestSequenceTracking:
    def test_contiguous_seq_applies(self, ws):
        _run(ws._handle_message(_snap(seq=1, yes=[["0.4900", "10"]])))
        _run(ws._handle_message(_delta(seq=2, delta="5")))
        _run(ws._handle_message(_delta(seq=3, delta="5")))
        book = ws.books["MKT"]
        assert book.is_usable()
        assert _levels(book.yes_bids) == [(49, 20.0)]
        assert book.last_seq == 3

    def test_any_gap_marks_stale_and_drops_deltas(self, ws):
        _run(ws._handle_message(_snap(seq=1, yes=[["0.4900", "10"]])))
        _run(ws._handle_message(_delta(seq=3, delta="5")))   # seq 2 lost
        book = ws.books["MKT"]
        assert book.stale and not book.is_usable()
        assert book.gap_count == 1
        assert _levels(book.yes_bids) == [(49, 10.0)]         # gap delta NOT applied
        _run(ws._handle_message(_delta(seq=4, delta="5")))   # still stale → dropped
        assert _levels(book.yes_bids) == [(49, 10.0)]

    def test_gap_triggers_unsubscribe_and_resubscribe(self, ws):
        _run(ws._handle_message(_snap(seq=1)))
        _run(ws._handle_message(_delta(seq=5)))
        cmds = [c["cmd"] for c in ws._ws.sent]
        assert cmds == ["unsubscribe", "subscribe"]
        assert ws._ws.sent[0]["params"]["sids"] == [7]
        assert ws._ws.sent[1]["params"]["market_tickers"] == ["MKT"]

    def test_resubscribe_throttled(self, ws):
        _run(ws._handle_message(_snap(seq=1)))
        _run(ws._handle_message(_delta(seq=5)))
        n = len(ws._ws.sent)
        _run(ws._handle_message(_delta(seq=9)))
        assert len(ws._ws.sent) == n

    def test_snapshot_clears_stale_and_rebases(self, ws):
        _run(ws._handle_message(_snap(seq=1, yes=[["0.4900", "10"]])))
        _run(ws._handle_message(_delta(seq=3)))
        assert ws.books["MKT"].stale
        # fresh subscription → new sid, seq restarts
        _run(ws._handle_message(_snap(sid=8, seq=1, yes=[["0.5000", "3"]])))
        book = ws.books["MKT"]
        assert book.is_usable()
        assert book.sid == 8
        assert _levels(book.yes_bids) == [(50, 3.0)]
        _run(ws._handle_message(_delta(sid=8, seq=2, price="0.5000", delta="1")))
        assert _levels(book.yes_bids) == [(50, 4.0)]

    def test_deltas_from_superseded_sid_ignored(self, ws):
        _run(ws._handle_message(_snap(sid=7, seq=1, yes=[["0.4900", "10"]])))
        _run(ws._handle_message(_snap(sid=8, seq=1, yes=[["0.4900", "10"]])))
        # late delta on the old sid (contiguous for sid 7, but sid 7 is dead)
        _run(ws._handle_message(_delta(sid=7, seq=2, delta="100")))
        assert _levels(ws.books["MKT"].yes_bids) == [(49, 10.0)]

    def test_duplicate_seq_dropped(self, ws):
        _run(ws._handle_message(_snap(seq=1, yes=[["0.4900", "10"]])))
        _run(ws._handle_message(_delta(seq=2, delta="5")))
        _run(ws._handle_message(_delta(seq=2, delta="5")))
        assert _levels(ws.books["MKT"].yes_bids) == [(49, 15.0)]

    def test_seq_is_per_subscription_not_per_market(self, ws):
        """Two markets on one sid share a counter. A gap seen on market B
        means market A's book may have lost a delta too — both go stale."""
        ws.books["MKT2"] = BookState(market_ticker="MKT2")
        _run(ws._handle_message(_snap("MKT", sid=7, seq=1)))
        _run(ws._handle_message(_snap("MKT2", sid=7, seq=2)))
        _run(ws._handle_message(_delta("MKT", sid=7, seq=3)))
        _run(ws._handle_message(_delta("MKT2", sid=7, seq=4)))
        # interleaved per-market seqs are NOT gaps at the sid level
        assert not ws.books["MKT"].stale and not ws.books["MKT2"].stale
        _run(ws._handle_message(_delta("MKT2", sid=7, seq=6)))   # 5 lost
        assert ws.books["MKT"].stale and ws.books["MKT2"].stale
        assert sorted(ws._ws.sent[-1]["params"]["market_tickers"]) == ["MKT", "MKT2"]

    def test_stale_transition_notifies_consumers_once(self, ws):
        seen = []

        async def cb(book):
            seen.append((book.market_ticker, book.stale))

        ws.on_update(cb)
        _run(ws._handle_message(_snap(seq=1)))
        _run(ws._handle_message(_delta(seq=3)))
        _run(ws._handle_message(_delta(seq=4)))
        assert seen == [("MKT", False), ("MKT", True)]

    def test_subscribed_ack_maps_sid_to_tickers(self, ws):
        cmd_id = _run(ws.subscribe_orderbook(["A", "B"]))
        _run(ws._handle_message(json.dumps(
            {"type": "subscribed", "id": cmd_id, "msg": {"channel": "orderbook_delta", "sid": 42}})))
        assert ws._sid_to_tickers[42] == ["A", "B"]

    def test_tolerance_setting_respected(self, ws, monkeypatch):
        monkeypatch.setattr(kw.settings, "WS_SEQ_GAP_TOLERANCE", 2, raising=False)
        _run(ws._handle_message(_snap(seq=1, yes=[["0.4900", "10"]])))
        _run(ws._handle_message(_delta(seq=4, delta="1")))   # gap of 2 ≤ tol
        assert not ws.books["MKT"].stale
        _run(ws._handle_message(_delta(seq=8, delta="1")))   # gap of 3 > tol
        assert ws.books["MKT"].stale


# ── Replay ────────────────────────────────────────────────────────────────

class TestReplay:
    def test_official_format_replay_matches_expected_book(self, ws):
        """Replay a captured-format sequence and compare against the book the
        venue would hold. Sizes are fixed-point (fractional) throughout."""
        stream = [
            _snap(seq=1,
                  yes=[["0.4700", "250.00"], ["0.4800", "120.50"], ["0.4900", "40.00"]],
                  no=[["0.5000", "30.00"], ["0.4900", "80.25"]]),
            _delta(seq=2, side="yes", price="0.4900", delta="-40.00"),   # top yes level removed
            _delta(seq=3, side="no",  price="0.5100", delta="15.75"),    # new best no
            _delta(seq=4, side="yes", price="0.4800", delta="0.50"),
            _delta(seq=5, side="no",  price="0.4900", delta="-80.25"),
            _delta(seq=6, side="yes", price="0.4600", delta="1000.00"),
        ]
        for raw in stream:
            _run(ws._handle_message(raw))
        book = ws.books["MKT"]
        assert book.is_usable()
        assert _levels(book.yes_bids) == [(48, 121.0), (47, 250.0), (46, 1000.0)]
        assert _levels(book.no_bids) == [(51, 15.75), (50, 30.0)]
        assert _levels(book.yes_asks) == [(49, 15.75), (50, 30.0)]
        assert _levels(book.no_asks) == [(52, 121.0), (53, 250.0), (54, 1000.0)]
        assert book.spread_cents() == 1
        assert book.last_seq == 6
        assert book.delta_count == 5
