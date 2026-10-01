"""Production read-only Kalshi client. Order methods make zero network calls."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from execution.quote_manager import QuoteManager, RestingOrder
from mm.safety.groups import SafeSender
from mm.unattended.loop import DemoPoster
from mm.venues.kalshi import KalshiAdapter
from mm.venues.kalshi_rest import KalshiRestTransport
from mm.venues.readonly import (
    DEMO_BOOKS_FLAG,
    PROD_BOOKS_FLAG,
    PROD_REST,
    PROD_WS,
    ReadOnlyDataError,
    ReadOnlyKalshiTransport,
    ReadOnlyMarketSocket,
    ReadOnlyViolation,
    book_source,
)


class _Net:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("network")


class _Key:
    def sign(self, data, pad, alg):
        return b"sig"


def _reader(net: _Net) -> ReadOnlyKalshiTransport:
    return ReadOnlyKalshiTransport(
        api_key="read-key", private_key=_Key(), session=net, base_url=PROD_REST,
    )


def test_every_order_method_refuses_with_no_network(caplog):
    caplog.set_level(logging.ERROR)
    net = _Net()
    reader = _reader(net)
    sock = ReadOnlyMarketSocket(api_key="read-key", private_key=_Key(), url=PROD_WS)
    attempts = [
        lambda: reader.request("POST", "/markets"),
        lambda: reader.request("PUT", "/markets/T"),
        lambda: reader.request("PATCH", "/markets/T"),
        lambda: reader.request("DELETE", "/markets/T"),
        lambda: reader.request("GET", "/portfolio/orders"),
        lambda: reader.request("GET", "/portfolio/fills"),
        lambda: reader.request("GET", "/portfolio/positions"),
        lambda: reader.request("GET", "/portfolio/balance"),
        lambda: reader.request("GET", "/portfolio/order_groups/create"),
        lambda: reader.post("/portfolio/events/orders", {"count": "1.00"}),
        lambda: reader.put("/portfolio/order_groups/g/trigger"),
        lambda: reader.patch("/markets/T"),
        lambda: reader.delete("/portfolio/events/orders/1"),
        lambda: reader.place("MKT", "yes", 50, 1),
        lambda: reader.cancel("order-1"),
        lambda: reader.amend("order-1"),
        lambda: reader.decrease("order-1", 1),
        lambda: reader.create_order_group(10),
        lambda: reader.trigger_order_group("group-1"),
        lambda: sock.command(["fill"], ["MKT"]),
        lambda: sock.command(["order"], ["MKT"]),
        lambda: sock.command(["positions"], ["MKT"]),
        lambda: sock.command(["user_orders"], ["MKT"]),
        lambda: sock.command(["orderbook_delta", "fill"], ["MKT"]),
    ]
    for attempt in attempts:
        with pytest.raises(ReadOnlyViolation):
            attempt()
    assert net.calls == 0
    assert sock.connects == 0
    assert "refused" in caplog.text


def test_order_paths_cannot_hold_the_reader(tmp_path):
    net = _Net()
    reader = _reader(net)
    assert not isinstance(reader, KalshiRestTransport)
    assert reader.allow_production is False
    assert reader.writes_orders is False
    with pytest.raises(ReadOnlyViolation):
        KalshiAdapter(transport=reader, paper=True)
    with pytest.raises(ReadOnlyViolation):
        SafeSender(SimpleNamespace(transport=reader))
    with pytest.raises(ReadOnlyViolation):
        DemoPoster("demo-api.kalshi.co", reader.request)
    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
    qm.paper = False
    qm.client = reader
    order = RestingOrder(
        order_id="o", market_ticker="MKT", side="yes", price_cents=50,
        size_contracts=10, placed_at=0.0,
    )
    with pytest.raises(ReadOnlyViolation):
        qm._decrease_order(order, 1)
    with pytest.raises(ReadOnlyViolation):
        qm._amend_order(order, 49, 10)
    with pytest.raises(ReadOnlyViolation):
        qm._cancel_order(order)
    assert net.calls == 0


def test_http_and_network_errors_are_not_a_hard_stop(caplog):
    """4xx/5xx and a dropped connection stay in the data path.

    A write is still a process exit. The log line for the HTTP failure is
    a warning, not the refusal that systemd treats as a crash.
    """
    caplog.set_level(logging.WARNING)

    class Down:
        def request(self, *args, **kwargs):
            return SimpleNamespace(status_code=503, json=lambda: {})

    reader = ReadOnlyKalshiTransport(
        api_key="read-key", private_key=_Key(), session=Down(), base_url=PROD_REST,
    )
    with pytest.raises(ReadOnlyDataError) as info:
        reader.get("/incentive_programs", params={"status": "active"})
    assert "503" in str(info.value)
    assert "refused" not in caplog.text

    class Drop:
        def request(self, *args, **kwargs):
            raise ConnectionError("reset")

    dropped = ReadOnlyKalshiTransport(
        api_key="read-key", private_key=_Key(), session=Drop(), base_url=PROD_REST,
    )
    with pytest.raises(ReadOnlyDataError):
        dropped.get("/incentive_programs")
    with pytest.raises(ReadOnlyViolation):
        reader.post("/portfolio/events/orders", {"count": "1.00"})


def test_public_get_is_allowed_and_demo_books_are_labeled(tmp_path, monkeypatch):
    seen = []

    class Ok:
        def request(self, method, url, headers=None, data=None, timeout=10):
            seen.append((method, url))
            return SimpleNamespace(status_code=200, json=lambda: {"markets": []})

    reader = ReadOnlyKalshiTransport(
        api_key="read-key", private_key=_Key(), session=Ok(), base_url=PROD_REST,
    )
    body = reader.get("/markets", params={"limit": "1"})
    assert body == {"markets": []}
    assert seen[0][0] == "GET"
    assert seen[0][1].startswith("https://api.elections.kalshi.com/trade-api/v2/markets")
    cmd = ReadOnlyMarketSocket(api_key="read-key", private_key=_Key()).command(
        ["orderbook_delta", "ticker", "trade"], ["MKT"],
    )
    assert cmd["params"]["channels"] == ["orderbook_delta", "ticker", "trade"]
    monkeypatch.delenv("KALSHI_PROD_READ_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PROD_READ_KEY_PATH", raising=False)
    assert book_source()["flag"] == DEMO_BOOKS_FLAG
    pem = tmp_path / "read.pem"
    pem.write_text("not-a-real-key\n", encoding="utf-8")
    monkeypatch.setenv("KALSHI_PROD_READ_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PROD_READ_KEY_PATH", str(pem))
    src = book_source()
    assert src["flag"] == PROD_BOOKS_FLAG
    assert src["representative"] is True
    assert src["ws_url"] == PROD_WS
    assert book_source(force_demo=True)["flag"] == DEMO_BOOKS_FLAG
