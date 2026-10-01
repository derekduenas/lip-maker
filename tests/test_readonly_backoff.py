"""Read-only book driver: transient errors back off, refusals hard-stop."""
import asyncio
from types import SimpleNamespace

import pytest
import requests

from mm.unattended import loop as L
from mm.venues import readonly as R


def _src(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "k.pem"
    pem.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    return {"key_id": "test", "key_path": str(pem), "ws_url": R.PROD_WS}


def test_http_error_is_subclass_and_transient():
    assert issubclass(R.ReadOnlyHTTPError, R.ReadOnlyViolation)
    assert R.ReadOnlyHTTPError in L.readonly_transient_types()
    assert R.ReadOnlyViolation not in L.readonly_transient_types()


def test_transport_http_error_raises_http_subclass():
    sess = SimpleNamespace(request=lambda *a, **k: SimpleNamespace(status_code=503, json=lambda: {}))
    t = R.ReadOnlyKalshiTransport(api_key="x", private_key=_FakeKey(), session=sess)
    with pytest.raises(R.ReadOnlyHTTPError) as e:
        t.get("/markets")
    assert e.value.status == 503 and e.value.code == 3


def test_write_route_still_plain_refusal():
    sess = SimpleNamespace(request=lambda *a, **k: pytest.fail("network used"))
    t = R.ReadOnlyKalshiTransport(api_key="x", private_key=_FakeKey(), session=sess)
    for call in (lambda: t.get("/portfolio/orders"), lambda: t.post("/portfolio/orders", {})):
        with pytest.raises(R.ReadOnlyViolation) as e:
            call()
        assert not isinstance(e.value, R.ReadOnlyHTTPError)


class _FakeKey:
    def sign(self, *a, **k):
        return b"sig"


def test_backoff_retries_transient_then_refusal_propagates(tmp_path, monkeypatch):
    calls = []
    errs = [R.ReadOnlyHTTPError("GET x HTTP 500", 500), requests.ConnectionError("down"),
            R.ReadOnlyViolation("POST /portfolio/orders")]

    async def fake_session(source, key, session, on_frame, state, **kw):
        calls.append(1)
        raise errs[len(calls) - 1]

    naps = []

    async def fake_sleep(s):
        naps.append(s)

    monkeypatch.setattr(L, "_readonly_books_session", fake_session)
    with pytest.raises(R.ReadOnlyViolation) as e:
        asyncio.run(L.drive_readonly_books(_src(tmp_path), lambda m: None, sleep=fake_sleep))
    assert not isinstance(e.value, R.ReadOnlyHTTPError)
    assert len(calls) == 3 and naps == [5.0, 10.0]


def test_backoff_caps_at_120(tmp_path, monkeypatch):
    async def fake_session(*a, **kw):
        raise R.ReadOnlyHTTPError("GET x HTTP 502", 502)

    naps = []

    async def fake_sleep(s):
        naps.append(s)

    monkeypatch.setattr(L, "_readonly_books_session", fake_session)
    with pytest.raises(R.ReadOnlyHTTPError):
        asyncio.run(L.drive_readonly_books(_src(tmp_path), lambda m: None,
                                           sleep=fake_sleep, max_attempts=8))
    assert naps == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0, 120.0]


def test_session_is_passed_to_transport(tmp_path, monkeypatch):
    seen = {}

    class T(R.ReadOnlyKalshiTransport):
        def __init__(self, **kw):
            seen["session"] = kw.get("session")
            raise R.ReadOnlyViolation("stop")

    monkeypatch.setattr(R, "ReadOnlyKalshiTransport", T)
    with pytest.raises(R.ReadOnlyViolation):
        asyncio.run(L.drive_readonly_books(_src(tmp_path), lambda m: None))
    assert isinstance(seen["session"], requests.Session)
