"""Ops review item A: PM US order price is always the YES (long) side.

docs.polymarket.us/api-reference/orders/overview (fetched 2026-10-01):
  "The `price.value` field always represents the long side's price,
   regardless of which order intent you use."
  "To trade the NO side at any price X, set `price.value = 1.00 - X`."
  | Buy Iowa at 0.83 | ORDER_INTENT_BUY_SHORT | 0.17 |

So a BUY_SHORT that buys NO at 0.40 must carry price.value 0.60. The old
builders sent 0.40, which is a YES sell at 0.40: below a 0.58 bid it
crosses and takes.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mm.venues.pmus import PMUSAdapter, pmus_wire_price_cents

ROOT = Path(__file__).resolve().parent.parent
QM_PATH = ROOT / "polymarket" / "execution" / "pm_quote_manager.py"
PQ_PATH = ROOT / "polymarket" / "tools" / "paper_quote.py"
LT_PATH = ROOT / "polymarket" / "tools" / "live_test.py"
AUTH_PATH = ROOT / "polymarket" / "execution" / "pm_auth.py"


def _stub_sdk(monkeypatch):
    stub = types.ModuleType("polymarket_us")
    stub.PolymarketUS = object
    monkeypatch.setitem(sys.modules, "polymarket_us", stub)


def _load(monkeypatch, path, name):
    _stub_sdk(monkeypatch)
    # The legacy tools import ``execution.pm_auth`` with polymarket/ on the
    # path; here ``execution`` is the repo-root package, so stub the helper.
    helper = types.ModuleType("execution.pm_auth")
    helper._load_dotenv_simple = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "execution.pm_auth", helper)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------- mm adapter
def test_wire_price_helper():
    assert pmus_wire_price_cents("ORDER_INTENT_BUY_LONG", 49) == 49
    assert pmus_wire_price_cents("ORDER_INTENT_BUY_SHORT", 40) == 60
    assert pmus_wire_price_cents("ORDER_INTENT_SELL_SHORT", 83) == 17
    with pytest.raises(ValueError):
        pmus_wire_price_cents("ORDER_INTENT_BUY_SHORT", 0)
    with pytest.raises(ValueError):
        pmus_wire_price_cents("ORDER_INTENT_BUY_SHORT", 100)
    with pytest.raises(ValueError):
        pmus_wire_price_cents("ORDER_INTENT_MYSTERY", 40)


def test_adapter_buy_short_body_carries_yes_side_price():
    a = PMUSAdapter(paper=True)
    r = a.place("m", intent="ORDER_INTENT_BUY_SHORT", price_cents=40, quantity=5, now=1.0)
    assert r["ok"] and r["body"]["price"]["value"] == "0.60"
    assert r["body"]["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert r["no_price_cents"] == 40 and r["wire_price_cents"] == 60
    long_ = a.place("m", intent="ORDER_INTENT_BUY_LONG", price_cents=49, quantity=5, now=1.0)
    assert long_["body"]["price"]["value"] == "0.49"


def test_adapter_docs_example_iowa():
    a = PMUSAdapter(paper=True)
    r = a.place("usc-iowa", intent="ORDER_INTENT_BUY_SHORT", price_cents=83, quantity=1, now=1.0)
    assert r["body"]["price"]["value"] == "0.17"


def test_adapter_engine_place_buy_short():
    a = PMUSAdapter(paper=True)
    r = a.engine_place("m", intent="ORDER_INTENT_BUY_SHORT", price_cents=30, quantity=2, now=1.0)
    assert r["paper"] and r["body"]["price"]["value"] == "0.70"


def test_adapter_modify_remembers_short_intent():
    a = PMUSAdapter(paper=True)
    placed = a.place("m", intent="ORDER_INTENT_BUY_SHORT", price_cents=40, quantity=5, now=1.0)
    mod = a.modify(placed["order_id"], price_cents=41, quantity=5, now=1.1)
    assert mod["ok"] and mod["body"]["price"]["value"] == "0.59"


def test_adapter_modify_unknown_intent_refuses():
    a = PMUSAdapter(paper=True)
    mod = a.modify("venue-id-not-ours", price_cents=41, quantity=5, now=1.1)
    assert mod["ok"] is False and "intent" in mod["error"]
    assert a.sent == []
    ok = a.modify("venue-id-not-ours", price_cents=41, quantity=5, now=1.1,
                  intent="ORDER_INTENT_BUY_SHORT")
    assert ok["ok"] and ok["body"]["price"]["value"] == "0.59"


def test_adapter_live_still_refuses_buy_short():
    transport = MagicMock()
    a = PMUSAdapter(transport=transport, paper=False)
    r = a.place("m", intent="ORDER_INTENT_BUY_SHORT", price_cents=40, quantity=5, now=1.0)
    assert r["ok"] is False and "live_blocked" in r["error"]
    transport.request.assert_not_called()


# ------------------------------------------------------- legacy runner path
@pytest.fixture
def qm_mod(monkeypatch):
    return _load(monkeypatch, QM_PATH, "_ops_pm_qm")


def _bodies(client, intent):
    return [c.args[0]["request"] for c in client.orders.preview.call_args_list
            if c.args[0]["request"]["intent"] == intent]


def test_quote_manager_sends_yes_side_price_for_buy_short(qm_mod):
    client = MagicMock()
    qm = qm_mod.PMQuoteManager(client, paper=True)
    # run_pm builds no_price = 1 - yes_ask: yes 0.58/0.60 -> NO bid 0.40
    t = qm_mod.QuoteTarget(slug="m", yes_price=0.58, no_price=0.40, quantity=10)
    qm.reconcile(t)
    (short,) = _bodies(client, "ORDER_INTENT_BUY_SHORT")
    assert short["price"]["value"] == "0.600"      # sells YES at the ask: rests
    (long_,) = _bodies(client, "ORDER_INTENT_BUY_LONG")
    assert long_["price"]["value"] == "0.580"
    # Internal state stays in NO terms so the next reconcile compares like with like.
    no = qm._existing_for_intent("m", "ORDER_INTENT_BUY_SHORT")
    assert no.price == pytest.approx(0.40)


def test_quote_manager_wire_price_round_trip(qm_mod):
    assert qm_mod.wire_price("ORDER_INTENT_BUY_SHORT", 0.40) == pytest.approx(0.60)
    assert qm_mod.wire_price("ORDER_INTENT_BUY_LONG", 0.40) == pytest.approx(0.40)
    assert qm_mod.outcome_price("ORDER_INTENT_BUY_SHORT", 0.60) == pytest.approx(0.40)
    assert qm_mod.outcome_price("ORDER_INTENT_SELL_LONG", 0.60) == pytest.approx(0.60)


def test_quote_manager_shield_uses_yes_side(qm_mod, monkeypatch):
    from polymarket.execution import pm_book_gate
    monkeypatch.setitem(sys.modules, "execution.pm_book_gate", pm_book_gate)
    qm = qm_mod.PMQuoteManager(MagicMock(), paper=True)
    chk = qm._virtual_post_only_check
    # NO bid 0.40 -> YES sell 0.60 against bid 0.58: rests
    ok, _ = chk("m", "ORDER_INTENT_BUY_SHORT", 0.40, ws_bid=0.58, ws_ask=0.60, ws_age_s=0.1)
    assert ok
    # NO bid 0.45 -> YES sell 0.55 <= bid 0.58: would take
    ok, why = chk("m", "ORDER_INTENT_BUY_SHORT", 0.45, ws_bid=0.58, ws_ask=0.60, ws_age_s=0.1)
    assert not ok and "0.550" in why
    ok, _ = chk("m", "ORDER_INTENT_BUY_LONG", 0.60, ws_bid=0.58, ws_ask=0.60, ws_age_s=0.1)
    assert not ok


# ------------------------------------------------------------ legacy tools
def test_paper_quote_preview_no_leg_uses_yes_ask(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "polymarket"))
    pq = _load(monkeypatch, PQ_PATH, "_ops_pm_pq")
    client = MagicMock()
    client.markets.bbo.return_value = {"marketData": {
        "bestBid": {"value": "0.58"}, "bestAsk": {"value": "0.60"}}}
    out = pq.preview_quote(client, "m", 10, verbose=False)
    assert out["no_request"]["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert out["no_request"]["price"]["value"] == "0.600"
    assert out["no_price"] == pytest.approx(0.40)
    assert out["yes_request"]["price"]["value"] == "0.580"


def test_pm_auth_buy_no_converts(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "polymarket"))
    auth = _load(monkeypatch, AUTH_PATH, "_ops_pm_auth")
    obj = auth.PolymarketClient.__new__(auth.PolymarketClient)
    sent = {}
    obj.post = lambda path, body=None: sent.update(path=path, body=body) or {}
    obj.buy_no("m", 0.40, 3)
    assert sent["body"]["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert sent["body"]["price"]["value"] == "0.600"


def _run_live_test(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "polymarket"))
    lt = _load(monkeypatch, LT_PATH, "_ops_pm_lt")
    client = MagicMock()
    client.account.balances.return_value = {"balances": [{"currentBalance": 100, "buyingPower": 100}]}
    client.orders.preview.return_value = {"order": {}}
    client.orders.create.return_value = {"order": {"id": "x"}}
    monkeypatch.setattr(lt, "get_client", lambda: client)
    monkeypatch.setattr(lt, "fetch_bbo", lambda c, s: {"yes_bid": 0.58, "yes_ask": 0.60, "current": 0.59})
    monkeypatch.setattr(lt, "confirm", lambda *a: True)
    monkeypatch.setattr(lt, "LOG_FILE", tmp_path / "live_orders.log")
    monkeypatch.setattr(sys, "argv", ["live_test.py", "--slug", "m", "--side", "no"])
    return lt.main(), client


def test_live_test_tool_no_side_previews_yes_side_price(monkeypatch, tmp_path):
    _rc, client = _run_live_test(monkeypatch, tmp_path)
    req = client.orders.preview.call_args.args[0]["request"]
    assert req["intent"] == "ORDER_INTENT_BUY_SHORT" and req["price"]["value"] == "0.600"
