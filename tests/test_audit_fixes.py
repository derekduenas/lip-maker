"""Regressions for the full-system audit."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from urllib.request import urlopen

import pytest

from config.settings import _parse_ramp_phase
from engine.account_ledger import AccountLedger
from engine.lip_discovery import _parse_program
from execution.kalshi_auth import parse_balance_usd
from execution.kalshi_ws import BookState, KalshiWS
from mm.types import Side
from execution.paper_fills import trades_url
from execution.quote_manager import QuoteManager, QuoteTarget, RestingOrder
from mm.cycle import run_paper_cycle, run_recording
from mm.ops import (
    ConfigError, backoff_seconds, configure_logging, redact,
    skew_is_excessive, validate_config,
)
from mm.selector import KalshiMarket, quote_economics
from mm.selector import PMQuote, pm_quote_economics
from mm.status_page import StatusPage, status_payload
from mm.unattended.feed import BookDriver
from mm.unattended.service import main
from mm.venues.base import TransportHTTPError
from mm.venues.kalshi import KalshiAdapter
from mm.venues.pmus import PMUSAdapter
from run_paper import record_fill_counts

ROOT = Path(__file__).resolve().parents[1]
AUG = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())


class _WS(KalshiWS):
    def _load_key(self):
        self._private_key = None


def test_delta_before_snapshot_is_dropped():
    ws = _WS(api_key="paper", private_key_path="/nonexistent",
             url="wss://demo-api.kalshi.co/trade-api/ws/v2")
    ws.books["MKT"] = BookState(market_ticker="MKT")
    import asyncio
    delta = json.dumps({
        "type": "orderbook_delta", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "MKT", "side": "yes",
                "price_dollars": "0.5000", "delta_fp": "10.00"},
    })
    asyncio.run(ws._handle_message(delta))
    assert ws.books["MKT"].yes_bids == []
    assert ws.books["MKT"].snapshot_count == 0


def test_balance_dollars_wins_over_the_cents_field():
    assert parse_balance_usd({"balance_dollars": "12.50", "balance": 1}) == 12.5
    assert parse_balance_usd({"balance": 250}) == 2.5


def test_reset_for_market_releases_the_reservation(tmp_path):
    acct = AccountLedger(opening_cash_usd=5000, mode="paper")
    acct.reserve("coid-1", market="MKT", program_id="p", price_cents=50, quantity=10)
    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"), account=acct)
    qm._update_quote_status = MagicMock()
    qm.resting["MKT"] = [RestingOrder(
        "oid", "MKT", "yes", 50, 10, 0.0, client_order_id="coid-1",
    )]
    qm.reset_for_market("MKT")
    assert acct.state().reserved_usd == 0
    assert "MKT" not in qm.resting


def test_uncertainty_on_one_market_does_not_block_another(tmp_path):
    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
    qm.uncertain_markets = {"OTHER"}
    qm._reconcile_locked = lambda target: {"action": "quoted", "market": target.market_ticker}
    assert qm.reconcile(QuoteTarget("MINE", 50, 50, 10))["action"] == "quoted"
    qm.uncertain_markets = {"MINE"}
    assert qm.reconcile(QuoteTarget("MINE", 50, 50, 10))["reason"] == "ORDER_STATE_UNCERTAIN"


def test_parsed_resting_order_keeps_its_shard():
    order = QuoteManager._parse_live_order({
        "order_id": "v", "ticker": "M", "status": "resting",
        "side": "yes", "book_side": "bid",
        "yes_price_dollars": "0.4000", "remaining_count_fp": "5.00",
        "exchange_index": 2,
    })
    assert order is not None
    assert order.exchange_index == 2
    assert order.side == "yes"


def test_amend_posts_the_shard_it_was_given():
    adapter = KalshiAdapter(paper=True)
    adapter.amend("oid", market="MKT", side=Side.YES, price_cents=40,
                  total_count=1, exchange_index=3, now=1.0)
    assert adapter.sent[-1]["body"]["exchange_index"] == 3


def test_decrease_posts_exchange_index(tmp_path, monkeypatch):
    import execution.order_request as orq
    monkeypatch.setattr(orq, "KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED", True)
    qm = QuoteManager(paper=True, db_path=str(tmp_path / "q.db"))
    qm.paper = False
    posted = {}
    qm.client = MagicMock()
    qm.client.post.side_effect = lambda path, body: posted.update(path=path, body=body) or {}
    order = RestingOrder("oid", "MKT", "yes", 40, 5, 0.0, paper=False, exchange_index=2)
    assert qm._decrease_order(order, 2) is True
    assert posted["body"]["exchange_index"] == 2
    assert posted["body"]["market_ticker"] == "MKT"


def test_fill_status_counts_do_not_replace_ticker_counts():
    status, by_ticker = {}, {}
    record_fill_counts(status, by_ticker, "applied", "KXBRENT-26OCT07")
    record_fill_counts(status, by_ticker, "duplicate", "KXBRENT-26OCT07")
    assert status["applied"] == 1 and status["duplicate"] == 1
    assert by_ticker == {"KXBRENT-26OCT07": 1}


def test_quote_price_is_the_reference_when_the_book_has_one():
    market = KalshiMarket(
        market="KXBRENT-26OCT07", series="KXBRENT",
        period_reward_usd=100, period_seconds=86400, seconds_left=86400,
        discount_factor=0.5, target_size=100,
        yes_bids=[(60, 5), (50, 100)], no_bids=[(60, 5), (50, 100)],
        days_to_settle=3, exchange_index=2, shard_cash_usd=10_000,
    )
    _net, _cap, _share, yes_c, no_c = quote_economics(market, 10)
    assert yes_c == 50 and no_c == 50


def test_no_side_reference_move_requotes():
    fired = []
    driver = BookDriver({"M": 100}, lambda market, price, latency: fired.append((market, price)))
    driver.on_book("M", yes_bids=[(50, 30)], no_bids=[(40, 30)], now=1.0)
    driver.on_book("M", yes_bids=[(50, 30)], no_bids=[(41, 30)], now=1.1)
    assert fired == [("M", 41)]


def test_pm_max_spread_and_competition_change_the_reward():
    base = PMQuote("m", reward_pool_usd=100, n_markets=1, period_seconds=86400,
                   our_bid=0.49, our_ask=0.51, our_size=100, target_size=100)
    wide = PMQuote("m", reward_pool_usd=100, n_markets=1, period_seconds=86400,
                   our_bid=0.40, our_ask=0.60, our_size=100, target_size=100,
                   max_spread_usd=0.001)
    crowded = PMQuote("m", reward_pool_usd=100, n_markets=1, period_seconds=86400,
                      our_bid=0.49, our_ask=0.51, our_size=100, target_size=100,
                      competing_bids=[(0.49, 100)], competing_asks=[(0.51, 100)])
    _n, _c, _p, reward, _r = pm_quote_economics(base)
    _n2, _c2, _p2, wide_reward, _r2 = pm_quote_economics(wide)
    _n3, _c3, _p3, crowded_reward, _r3 = pm_quote_economics(crowded)
    assert reward > 0
    assert wide_reward == 0
    assert crowded_reward < reward


def test_incentive_fetch_stops_at_five_per_second():
    class Transport:
        def __init__(self):
            self.n = 0

        def request(self, method, path, **kwargs):
            self.n += 1
            return {"markets": [{"slug": "m"}]}

    transport = Transport()
    adapter = PMUSAdapter(transport=transport, paper=False)
    for _ in range(7):
        adapter.incentives_cache = []
        adapter.incentives(now=0.0)
    assert transport.n == 5
    assert adapter.incentive_limited is True


def test_amend_respects_the_token_bucket_and_429_reports_backoff():
    adapter = KalshiAdapter(paper=True)
    ok = 0
    for _ in range(12):
        resp = adapter.amend("oid", market="MKT", side=Side.YES, price_cents=40,
                             total_count=1, now=0.0)
        if resp.get("ok"):
            ok += 1
    assert ok == 10

    class Boom:
        def request(self, *args, **kwargs):
            raise TransportHTTPError(429, {"message": "slow"})

    import execution.order_request as orq
    orq.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED = True
    try:
        live = KalshiAdapter(transport=Boom(), paper=False)
        blocked = live._call("POST", "/portfolio/events/orders", {"count": "1.00"},
                             cost=1, operation="place")
    finally:
        orq.KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED = False
    assert blocked["error"] == "rate_limited"
    assert blocked["backoff_s"] == backoff_seconds(429, 0)


def test_trades_url_uses_the_caller_base():
    assert trades_url("ticker=M", base="https://demo-api.kalshi.co/trade-api/v2").startswith(
        "https://demo-api.kalshi.co/")


def test_program_parse_converts_the_account_cap():
    parsed = _parse_program({
        "id": "p", "market_ticker": "KXBRENT-26OCT07",
        "period_reward": 1_000_000, "discount_factor_bps": 5000,
        "target_size_fp": "100", "paid_out": False,
        "start_date": "2026-08-01T00:00:00Z", "end_date": "2026-08-02T00:00:00Z",
        "max_reward_per_account": 100_000,
    })
    assert parsed["max_reward_usd"] == 10


def test_ramp_phase_rejects_text():
    with pytest.raises(RuntimeError):
        _parse_ramp_phase("later")


def test_clock_skew_secret_redaction_and_log_rotation(tmp_path):
    assert skew_is_excessive(100.0, 103.0)
    assert redact("BEGIN PRIVATE KEY abc") == "BEGIN PRIVATE KEY [redacted]"
    with pytest.raises(ConfigError):
        validate_config(paper=True, argv=["--key", "-----BEGIN PRIVATE KEY-----"])
    log = configure_logging(str(tmp_path / "lip.log"))
    log.info("hello")
    for handler in log.handlers:
        handler.flush()
    assert (tmp_path / "lip.log").exists()


def test_status_page_is_loopback_json():
    page = StatusPage(lambda: {"stage": "risk", "estimated_usd": "1.00", "markets": ["M"], "kill": None},
                      port=0)
    try:
        import threading
        thread = threading.Thread(target=page.serve_one, daemon=True)
        thread.start()
        with urlopen(f"http://127.0.0.1:{page.port}/status", timeout=2) as resp:
            body = json.loads(resp.read().decode())
        thread.join(timeout=2)
    finally:
        page.close()
    assert body["paper"] is True
    assert body["live_armed"] is False
    assert status_payload({})["live_armed"] is False


def test_paper_cycle_on_a_recording(tmp_path):
    program = {
        "kind": "program", "market": "KXBRENT-26OCT07", "series": "KXBRENT",
        "period_reward_usd": 86400, "period_seconds": 86400, "seconds_left": 86400,
        "discount_factor": 0.5, "target_size": 100, "days_to_settle": 3,
        "exchange_index": 2, "shard_cash_usd": 10000,
        "yes_bids": [[50, 100]], "no_bids": [[50, 100]],
    }
    books = [
        {"kind": "book", "ts": AUG + 0.2, "market": "KXBRENT-26OCT07",
         "yes_bid": 50, "yes_size": 100, "no_bid": 50, "no_size": 100},
        {"kind": "book", "ts": AUG + 1.2, "market": "KXBRENT-26OCT07",
         "yes_bid": 50, "yes_size": 100, "no_bid": 50, "no_size": 100,
         "exchange_ts": AUG + 1.2},
    ]
    credit = {"kind": "credit", "kind_reward": True}
    # The ledger parser wants kind=liquidity_reward on the entry itself.
    lines = [json.dumps(program), json.dumps(books[0]), json.dumps(books[1]),
             json.dumps({"kind": "credit", "source": "kalshi_api", "market": "KXBRENT-26OCT07",
                         "program_id": "KXBRENT-26OCT07", "amount_usd": "5",
                         "reward_kind": "liquidity_reward"})]
    # credits_from_ledger reads the entry's own kind. Store the credit fields
    # the parser accepts by putting them on a credit row the loader forwards.
    path = tmp_path / "books.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Loader treats kind=credit as a ledger entry only if we pass it through.
    # Rewrite the credit line to the shape credits_from_ledger accepts, and
    # teach the loader by calling run_paper_cycle with an explicit credit.
    from mm.cycle import load_recording
    markets, loaded_books, loaded_credits = load_recording(path)
    loaded_credits = [{
        "kind": "liquidity_reward", "source": "kalshi_api",
        "market": "KXBRENT-26OCT07", "program_id": "KXBRENT-26OCT07",
        "amount_usd": "1.00",
    }, {"kind": "balance", "amount_usd": "9", "source": "kalshi_api"}]
    report = run_paper_cycle(markets, loaded_books, credits=loaded_credits, bankroll=10_000, chunk=100)
    assert report["paper"] is True and report["live_armed"] is False
    assert report["quotes"] and report["quotes"][0]["paper"] is True
    assert Decimal(report["estimated_usd"]) == Decimal("1")
    assert Decimal(report["reconcile"]["matches"][0]) == Decimal("1")
    assert report["reconcile"]["rejected"] == 1
    assert report["factors"]["KXBRENT"] == pytest.approx(1.0)
    assert report["next_usd"]["KXBRENT-26OCT07"] == 0.0
    assert report["risk"][0]["allowed"] is True
    assert report["kill"] is None
    # CLI path: a recording whose credit row is already a liquidity_reward
    # is not a kind the loader knows. The loader stores kind=credit rows as
    # given, so write the parser shape under kind=credit and map it.
    cli = tmp_path / "cli.jsonl"
    credit_row = {
        "kind": "credit", "entry_kind": "liquidity_reward", "source": "kalshi_api",
        "market": "KXBRENT-26OCT07", "program_id": "KXBRENT-26OCT07",
        "amount_usd": "1.00",
    }
    ignored = {"kind": "credit", "entry_kind": "balance", "amount_usd": "9",
               "source": "kalshi_api"}
    cli.write_text("\n".join(json.dumps(row) for row in (
        program, books[0], books[1], credit_row, ignored)) + "\n", encoding="utf-8")
    out = tmp_path / "report.json"
    code = main(["--once", "--cycle", str(cli), "--report", str(out),
                 "--heartbeat", str(tmp_path / "hb"),
                 "--cancel-log", str(tmp_path / "cancel")])
    assert code == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["quotes"][0]["market"] == "KXBRENT-26OCT07"
    assert Decimal(saved["reconcile"]["matches"][0]) == Decimal("1")
    assert saved["reconcile"]["rejected"] == 1
    assert "cancel_all" in (tmp_path / "cancel").read_text(encoding="utf-8")


def test_deploy_script_stays_on_paper():
    script = (ROOT / "deploy" / "droplet" / "setup.sh").read_text(encoding="utf-8")
    env = (ROOT / "deploy" / "droplet" / "lip-maker.env.example").read_text(encoding="utf-8")
    unit = (ROOT / "deploy" / "lip-unattended.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=-/etc/lip-maker/lip-maker.env" in unit
    assert "LIP_PAPER=true" in unit
    assert "LIP_PAPER=true" in script or "LIP_PAPER=true" in env
    assert "ufw" in script
    assert "BEGIN PRIVATE KEY" not in script
    assert "BEGIN PRIVATE KEY" not in env
    assert "LIP_PAPER=false" not in env
