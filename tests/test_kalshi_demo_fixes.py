"""Fixes from the 2026-09-30 Kalshi demo wire (PR #2 follow-up)."""
from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from execution import order_request as orq
from mm.types import Side
from mm.venues.base import TransportHTTPError
from mm.venues.kalshi import KalshiAdapter
from mm.venues.kalshi_rest import (
    KalshiHostRejected, KalshiRestTransport, signing_path,
)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def kalshi_acked():
    previous = orq.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED
    orq.enable_kalshi_maker_only_enforcement(orq.KALSHI_POST_ONLY_ACK)
    try:
        yield
    finally:
        orq.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED = previous


class _Resp:
    def __init__(self, status, body, url):
        self.status_code = status
        self._body = body
        self.url = url
        self.text = "" if body is None else __import__("json").dumps(body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, *, status=200, body=None, url_override=None):
        self.calls = []
        self.status = status
        self.body = {} if body is None else body
        self.url_override = url_override

    def request(self, method, url, headers=None, data=None, timeout=None,
                allow_redirects=None):
        self.calls.append({
            "method": method, "url": url, "headers": headers, "data": data,
            "allow_redirects": allow_redirects,
        })
        return _Resp(self.status, self.body, self.url_override or url)


class _Routes:
    """Scripted transport. GET market/balance, then the write."""

    def __init__(self, write):
        self.write = write
        self.calls = []

    def request(self, method, path, body=None, params=None):
        self.calls.append({"method": method, "path": path, "body": body, "params": params})
        if method == "GET" and path.startswith("/markets/"):
            return {"market": {"exchange_index": 2, "ticker": path.rsplit("/", 1)[-1]}}
        if method == "GET" and path == "/portfolio/balance":
            return {"balance_dollars": "100.0000", "balance": 10000}
        return self.write(method, path, body, params)


class TestPostOnlyFlag:
    def test_defaults_stay_false(self):
        assert orq.MAKER_ONLY_ENFORCEMENT_VERIFIED is False
        assert orq.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED is False
        with pytest.raises(orq.LiveExecutionBlocked):
            orq.require_live_execution_allowed()
        with pytest.raises(orq.LiveExecutionBlocked):
            orq.require_live_execution_allowed(venue="kalshi")

    def test_ack_enables_kalshi_only(self, kalshi_acked):
        assert orq.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED is True
        assert orq.MAKER_ONLY_ENFORCEMENT_VERIFIED is False
        orq.require_live_execution_allowed(venue="kalshi")
        with pytest.raises(orq.LiveExecutionBlocked):
            orq.require_live_execution_allowed()

    def test_wrong_ack_does_not_flip_the_flag(self):
        with pytest.raises(orq.LiveExecutionBlocked):
            orq.enable_kalshi_maker_only_enforcement("yes")
        assert orq.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED is False


class TestParseResting:
    def test_book_side_ask_is_a_no_bid_not_a_yes_bid(self):
        # Observed: V2 ask comes back side=yes, book_side=ask, outcome_side=no.
        # Price 0.59 on the YES book is a NO bid at 41c, not YES at 59c.
        view = KalshiAdapter.parse_resting({
            "order_id": "v", "client_order_id": "c", "ticker": "KXRAIN-26SEP30-DTW",
            "side": "yes", "action": "sell", "book_side": "ask", "outcome_side": "no",
            "price": "0.5900", "remaining_count": "1.00", "status": "resting",
        })
        assert view.side == Side.NO and view.price_cents == 41

    def test_book_side_bid_stays_yes(self):
        view = KalshiAdapter.parse_resting({
            "order_id": "v", "ticker": "M", "side": "yes", "book_side": "bid",
            "price": "0.5300", "remaining_count": "2.00", "status": "resting",
        })
        assert view.side == Side.YES and view.price_cents == 53

    def test_quote_manager_uses_the_same_book_side(self):
        from execution.quote_manager import QuoteManager
        o = QuoteManager._parse_live_order({
            "order_id": "v", "ticker": "M", "status": "resting",
            "side": "yes", "book_side": "ask", "outcome_side": "no",
            "price": "0.5900", "remaining_count_fp": "1.00",
        })
        assert o.side == "no" and o.price_cents == 41


class TestExchangeErrors:
    def test_raising_post_only_cross_is_not_ok(self, kalshi_acked):
        def write(method, path, body, params):
            raise TransportHTTPError(400, {
                "error": {"code": "invalid_order", "message": "invalid order",
                          "details": "post only cross"},
            })
        a = KalshiAdapter(transport=_Routes(write), paper=False)
        resp = a.place("KXRAIN-26SEP30-DTW", Side.YES, 58, 1, best_opposing_bid_cents=40)
        assert resp["ok"] is False
        assert resp["error"] == "post_only_cross"
        assert resp.get("order_id", "") == ""

    def test_nonraising_error_body_is_not_an_empty_order_id(self, kalshi_acked):
        def write(method, path, body, params):
            return {"error": {"code": "invalid_order", "details": "post only cross"}}
        a = KalshiAdapter(transport=_Routes(write), paper=False)
        resp = a.place("KXRAIN-26SEP30-DTW", Side.YES, 40, 1, best_opposing_bid_cents=50)
        assert resp["ok"] is False and resp["error"] == "post_only_cross"
        assert resp.get("order_id", "") == ""

    def test_success_without_order_id_is_not_ok(self, kalshi_acked):
        a = KalshiAdapter(transport=_Routes(lambda *a, **k: {"fill_count": "0.00"}), paper=False)
        resp = a.place("KXRAIN-26SEP30-DTW", Side.YES, 40, 1, best_opposing_bid_cents=50)
        assert resp["ok"] is False and resp["error"] == "missing_order_id"

    def test_cancel_404_is_already_gone(self, kalshi_acked):
        class T:
            def request(self, method, path, body=None, params=None):
                raise TransportHTTPError(404, {"error": {"code": "not_found", "message": "not found"}})
        a = KalshiAdapter(transport=T(), paper=False)
        resp = a.cancel("oid", market="KXRAIN-26SEP30-DTW")
        assert resp["ok"] is True and resp["already_gone"] is True

    def test_amend_200_remaining_zero_is_dead(self, kalshi_acked):
        class T:
            def request(self, method, path, body=None, params=None):
                return {"order_id": "oid", "fill_count": "0.00", "remaining_count": "0.00"}
        a = KalshiAdapter(transport=T(), paper=False)
        resp = a.amend("oid", market="KXRAIN-26SEP30-DTW", side=Side.YES,
                       price_cents=58, total_count=1)
        assert resp["ok"] is False
        assert resp["dead"] is True
        assert resp["error"] == "post_only_cancelled"
        assert resp["order_id"] == "oid"


class TestRouting:
    def test_decrease_and_queue_carry_market_ticker(self, kalshi_acked):
        seen = []

        class T:
            def request(self, method, path, body=None, params=None):
                seen.append((method, path, body, params))
                if path.endswith("/queue_position"):
                    return {"queue_position_fp": "20754.36"}
                return {"order_id": "oid", "remaining_count": "1.00"}

        a = KalshiAdapter(transport=T(), paper=False)
        a.remember_shard("KXBTC-1", 2)
        dec = a.decrease("oid", 1, market="KXBTC-1")
        assert dec["ok"] is True
        assert seen[0][2]["market_ticker"] == "KXBTC-1"
        assert seen[0][2]["exchange_index"] == 2
        pos = a.queue_position("oid", market="KXBTC-1")
        assert pos == pytest.approx(20754.36)
        assert seen[1][3]["market_ticker"] == "KXBTC-1"
        assert seen[1][3]["exchange_index"] == 2

    def test_decrease_without_ticker_is_refused(self):
        a = KalshiAdapter(paper=True)
        assert a.decrease("oid", 1)["error"] == "market_ticker_required"
        assert a.sent == []

    def test_place_uses_market_shard_and_checks_balance(self, kalshi_acked):
        routes = _Routes(lambda *a, **k: {"order_id": "oid-1", "remaining_count": "1.00"})
        a = KalshiAdapter(transport=routes, paper=False)
        resp = a.place("KXBTC-1", Side.YES, 40, 1, best_opposing_bid_cents=50)
        assert resp["ok"] is True
        assert resp["body"]["exchange_index"] == 2
        gets = [c for c in routes.calls if c["method"] == "GET"]
        assert gets[0]["path"] == "/markets/KXBTC-1"
        assert gets[1]["params"]["exchange_index"] == 2

    def test_low_shard_balance_does_not_send(self, kalshi_acked):
        class T:
            def __init__(self):
                self.posts = 0
            def request(self, method, path, body=None, params=None):
                if method == "GET" and path.startswith("/markets/"):
                    return {"market": {"exchange_index": 3}}
                if method == "GET":
                    return {"balance_dollars": "0.50"}
                self.posts += 1
                return {"order_id": "should-not"}
        t = T()
        a = KalshiAdapter(transport=t, paper=False)
        resp = a.place("KXMLB-1", Side.YES, 40, 10, best_opposing_bid_cents=50)
        assert resp["ok"] is False
        assert resp["error"] == "insufficient_shard_balance"
        assert t.posts == 0

    def test_exchange_insufficient_shard_balance_is_typed(self, kalshi_acked):
        def write(method, path, body, params):
            raise TransportHTTPError(404, {
                "error": {"code": "insufficient_shard_balance",
                          "message": "insufficient shard balance",
                          "details": "Exchange user not found."},
            })
        a = KalshiAdapter(transport=_Routes(write), paper=False)
        resp = a.place("KXBTC-1", Side.YES, 40, 1, best_opposing_bid_cents=50)
        assert resp["ok"] is False
        assert resp["error"] == "insufficient_shard_balance"
        assert resp.get("order_id", "") == ""

    def test_order_group_lands_on_the_market_shard(self, kalshi_acked):
        seen = {}

        class T:
            def request(self, method, path, body=None, params=None):
                seen["body"] = body
                return {"order_group_id": "og-1", "exchange_index": body.get("exchange_index"),
                        "subaccount": 0}
        a = KalshiAdapter(transport=T(), paper=False)
        a.remember_shard("KXBTC-1", 2)
        spec = a.create_order_group(1, market="KXBTC-1")
        assert spec["ok"] is True
        assert seen["body"]["exchange_index"] == 2

    def test_order_group_without_a_shard_does_not_default_to_zero(self, kalshi_acked):
        a = KalshiAdapter(transport=_Routes(lambda *a, **k: {"order_group_id": "x"}), paper=False)
        resp = a.create_order_group(1)
        assert resp["ok"] is False and resp["error"] == "shard_unknown"


class TestSignedTransport:
    def test_signs_path_without_query_and_allows_demo(self):
        key = _key()
        session = _Session(body={"ok": True})
        transport = KalshiRestTransport(
            base_url="https://demo-api.kalshi.co/trade-api/v2",
            api_key="kid", private_key=key, session=session,
        )
        transport.request(
            "DELETE", "/portfolio/events/orders/oid",
            params={"market_ticker": "KXRAIN-26SEP30-DTW"},
        )
        assert transport.last_signed_path == "/trade-api/v2/portfolio/events/orders/oid"
        assert "?" not in transport.last_signed_path
        call = session.calls[0]
        assert call["allow_redirects"] is False
        assert "market_ticker=KXRAIN-26SEP30-DTW" in call["url"]
        headers = call["headers"]
        sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        ts = headers["KALSHI-ACCESS-TIMESTAMP"]
        good = f"{ts}DELETE{transport.last_signed_path}".encode()
        key.public_key().verify(
            sig, good,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        bad = f"{ts}DELETE{transport.last_signed_path}?market_ticker=KXRAIN-26SEP30-DTW".encode()
        with pytest.raises(InvalidSignature):
            key.public_key().verify(
                sig, bad,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=hashes.SHA256().digest_size),
                hashes.SHA256(),
            )

    def test_signing_path_helper_strips_query(self):
        assert signing_path("/portfolio/orders/x?market_ticker=T") == "/trade-api/v2/portfolio/orders/x"

    def test_production_host_requires_an_explicit_flag(self):
        key = _key()
        with pytest.raises(KalshiHostRejected):
            KalshiRestTransport(
                base_url="https://api.elections.kalshi.com/trade-api/v2",
                api_key="kid", private_key=key, session=_Session(),
            )
        KalshiRestTransport(
            base_url="https://api.elections.kalshi.com/trade-api/v2",
            api_key="kid", private_key=key, allow_production=True, session=_Session(),
        )

    def test_unknown_and_cleartext_hosts_are_refused(self):
        key = _key()
        with pytest.raises(KalshiHostRejected):
            KalshiRestTransport(
                base_url="http://demo-api.kalshi.co/trade-api/v2",
                api_key="kid", private_key=key, session=_Session(),
            )
        with pytest.raises(KalshiHostRejected):
            KalshiRestTransport(
                base_url="https://evil.example/trade-api/v2",
                api_key="kid", private_key=key, allow_production=True, session=_Session(),
            )

    def test_response_host_must_match_the_request(self):
        key = _key()
        session = _Session(url_override="https://api.elections.kalshi.com/trade-api/v2/x")
        transport = KalshiRestTransport(
            base_url="https://demo-api.kalshi.co/trade-api/v2",
            api_key="kid", private_key=key, session=session,
        )
        with pytest.raises(KalshiHostRejected):
            transport.request("GET", "/portfolio/balance")


class TestQuoteManagerAmendDeath:
    def test_http_200_remaining_zero_removes_the_quote(self, monkeypatch):
        from execution.quote_manager import QuoteManager, QuoteTarget, RestingOrder
        import time
        monkeypatch.setattr(
            "execution.quote_manager.require_live_execution_allowed", lambda: None)
        qm = QuoteManager(paper=True)
        qm.paper = False
        qm.client = type("C", (), {
            "post": lambda self, path, body: {
                "order_id": "OLD-YES-1", "remaining_count": "0.00", "fill_count": "0.00",
            },
        })()
        monkeypatch.setattr(qm, "_update_quote_status", lambda *a, **k: None)
        monkeypatch.setattr(qm, "_passes_safety", lambda t: (True, "ok"))
        monkeypatch.setattr(qm, "_refresh_inventory", lambda m: None)
        qm.inventory = {}
        now = time.time()
        qm.resting["TEST-MKT"] = [
            RestingOrder(order_id="OLD-YES-1", market_ticker="TEST-MKT", side="yes",
                         price_cents=40, size_contracts=25, placed_at=now, paper=False),
            RestingOrder(order_id="OLD-NO-1", market_ticker="TEST-MKT", side="no",
                         price_cents=58, size_contracts=25, placed_at=now, paper=False),
        ]
        target = QuoteTarget(market_ticker="TEST-MKT", yes_bid_cents=42,
                             no_bid_cents=58, size_contracts=25)
        actions = qm.reconcile(target)
        assert actions["cancelled"] == 1
        sides = [o.side for o in qm.resting.get("TEST-MKT", [])]
        assert sides == ["no"]
