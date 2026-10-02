"""Review fixes: V2 cancel route with routing params, cancel scope (ours vs all).

Fake opener; no network, no signing key.
"""
import io
import json
import urllib.error
import urllib.parse

import pytest

from mm.safety import lip_watchdog as wd


def _cfg(tmp_path, **extra):
    env = {"LIP_WD_STATE_DIR": str(tmp_path), "LIP_WD_LIVE_ARMED": "true", "LIP_PAPER": "false",
           "LIP_WD_KALSHI_KEY_ID": "k", "LIP_WD_KALSHI_KEY_PATH": "/nonexistent",
           "LIP_WD_KALSHI_REST": "https://example.invalid/trade-api/v2"}
    env.update({k: str(v) for k, v in extra.items()})
    return wd.Config(env)


ORDERS = [
    {"order_id": "o1", "client_order_id": "LIP-aaaa", "ticker": "KXA-26DEC-T1", "exchange_index": 2,
     "status": "resting"},
    {"order_id": "o2", "client_order_id": "LIP-bbbb", "ticker": "KXB-26DEC-T5", "exchange_index": 0,
     "status": "resting"},
    {"order_id": "w1", "client_order_id": "WX-cccc", "ticker": "KXHIGHNY-26OCT02-B70",
     "exchange_index": 1, "status": "resting"},
    {"order_id": "w2", "client_order_id": "", "ticker": "KXHIGHNY-26OCT02-B72",
     "exchange_index": 1, "status": "resting"},
]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeKalshi:
    def __init__(self, orders):
        self.orders = {o["order_id"]: dict(o) for o in orders}
        self.calls = []

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((req.get_method(), u.path, q))
        if req.get_method() == "GET" and u.path.endswith("/portfolio/orders"):
            return _Resp(json.dumps({"orders": list(self.orders.values()), "cursor": ""}).encode())
        if req.get_method() == "DELETE":
            oid = u.path.rsplit("/", 1)[-1]
            if oid not in self.orders:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            self.orders.pop(oid)
            return _Resp(json.dumps({"order_id": oid, "reduced_by": "1.00"}).encode())
        raise AssertionError(f"unexpected {req.get_method()} {u.path}")


@pytest.fixture(autouse=True)
def _no_sign(monkeypatch):
    monkeypatch.setattr(wd.KalshiCanceller, "_sign", lambda self, m, p: {})


def _deletes(fake):
    return [(p, q) for m, p, q in fake.calls if m == "DELETE"]


def test_v2_cancel_path_with_routing_params(tmp_path):
    fake = FakeKalshi(ORDERS)
    out = wd.KalshiCanceller(_cfg(tmp_path), opener=fake).cancel_all()
    dels = _deletes(fake)
    assert ("/trade-api/v2/portfolio/events/orders/o1",
            {"market_ticker": "KXA-26DEC-T1", "exchange_index": "2"}) in dels
    assert ("/trade-api/v2/portfolio/events/orders/o2",
            {"market_ticker": "KXB-26DEC-T5", "exchange_index": "0"}) in dels
    assert not any("/portfolio/orders/" in p for p, _q in dels), "legacy DELETE route used"
    assert out["ok"] and out["remaining"] == 0
    # verify-by-relisting still happens (2 GETs: list, verify)
    assert sum(1 for m, p, _q in fake.calls if m == "GET") == 2


def test_foreign_orders_not_cancelled_by_default(tmp_path):
    fake = FakeKalshi(ORDERS)
    out = wd.KalshiCanceller(_cfg(tmp_path), opener=fake).cancel_all()
    ids = {p.rsplit("/", 1)[-1] for p, _q in _deletes(fake)}
    assert ids == {"o1", "o2"}
    assert "w1" in fake.orders and "w2" in fake.orders
    assert out["ok"] and out["skipped_foreign"] == 2


def test_scope_all_cancels_everything(tmp_path):
    fake = FakeKalshi(ORDERS)
    out = wd.KalshiCanceller(_cfg(tmp_path, LIP_WD_CANCEL_SCOPE="all"), opener=fake).cancel_all()
    assert {p.rsplit("/", 1)[-1] for p, _q in _deletes(fake)} == {"o1", "o2", "w1", "w2"}
    assert out["ok"] and not fake.orders


def test_coid_prefix_override(tmp_path):
    fake = FakeKalshi(ORDERS)
    wd.KalshiCanceller(_cfg(tmp_path, LIP_WD_COID_PREFIX="WX-"), opener=fake).cancel_all()
    assert {p.rsplit("/", 1)[-1] for p, _q in _deletes(fake)} == {"w1"}


def test_order_without_ticker_is_not_sent_to_shard0(tmp_path):
    rows = [{"order_id": "o9", "client_order_id": "LIP-x", "status": "resting"}]
    fake = FakeKalshi(rows)
    out = wd.KalshiCanceller(_cfg(tmp_path), opener=fake).cancel_all()
    assert _deletes(fake) == []
    assert not out["ok"] and out["errors"] == 1 and out["remaining"] == 1


def test_missing_exchange_index_auto_routes_by_ticker(tmp_path):
    rows = [{"order_id": "o3", "client_order_id": "LIP-y", "ticker": "KXC-1", "status": "resting"}]
    fake = FakeKalshi(rows)
    out = wd.KalshiCanceller(_cfg(tmp_path), opener=fake).cancel_all()
    assert _deletes(fake) == [("/trade-api/v2/portfolio/events/orders/o3", {"market_ticker": "KXC-1"})]
    assert out["ok"]


def test_invalid_scope_falls_back_to_ours(tmp_path):
    assert _cfg(tmp_path, LIP_WD_CANCEL_SCOPE="everything").cancel_scope == "ours"
