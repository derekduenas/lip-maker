"""Compounding, shared PM US pools, and one cross-venue book."""
from __future__ import annotations

import base64

import pytest

from mm.compound import (
    LADDER_USD, BankrollLedger, MarketSample, ScaleLadder, capped_equity,
    observed_per_dollar, posterior, reallocate,
)
from mm.cross_risk import (
    CrossKill, Listing, Position, limit_breaches, match_markets, net_exposure,
)
from mm.selector import KalshiMarket, PMQuote, pm_quote_economics, rank_cross_venue
from mm.types import Side
from mm.venues.kalshi import KalshiAdapter
from mm.venues.pmus import PMUSAdapter
from polymarket.engine.pm_us_lip_scorer import (
    PMProgram, effective_pool_from_market, effective_reward_pool_usd,
    expected_payout_usd, with_shared_pools,
)


def test_shared_pool_is_divided_across_the_program():
    shares = [0.5] * 3600
    alone = expected_payout_usd(shares, reward_pool_usd=100.0, period_seconds=86400)
    shared = expected_payout_usd(
        shares, reward_pool_usd=100.0, period_seconds=86400, n_markets=12)
    assert shared == pytest.approx(alone / 12)
    assert effective_reward_pool_usd(100.0, 12) == pytest.approx(100 / 12)
    assert effective_pool_from_market({"pool_eff": 8.5}) == 8.5
    programs = [
        PMProgram(f"m{i}", "atp-day", "day_of", 100.0, 0.5, 100.0, None, None, "active")
        for i in range(12)
    ]
    marked = with_shared_pools(programs)
    assert {p.n_markets for p in marked} == {12}


def test_ledger_and_shrink_and_sample_gate():
    book = BankrollLedger(500)
    book.post(venue="kalshi", market="KXBRENT-26OCT07", series="KXBRENT",
              rewards_usd=4, fill_pnl_usd=-1)
    book.post(venue="pmus", market="btc-100k", series="btc", rewards_usd=2, fill_pnl_usd=1)
    assert book.net(venue="kalshi") == pytest.approx(3)
    assert book.net(series="btc") == pytest.approx(3)
    assert book.equity() == pytest.approx(506)
    assert observed_per_dollar(book, "KXBRENT-26OCT07", 100) == pytest.approx(0.03)
    assert posterior(1.0, 0.1, 0) == pytest.approx(0.1)
    assert posterior(1.0, 0.1, 10_000) == pytest.approx(1.0, rel=1e-3)

    grown = reallocate(
        [MarketSample("A", observed_per_dollar=2.0, prior_per_dollar=0.1,
                      n=10, previous_usd=10)],
        equity=1_000, peak=1_000, fraction=0.25, min_sample=5,
    )
    assert grown["A"] == pytest.approx(250)
    held = reallocate(
        [MarketSample("A", observed_per_dollar=2.0, prior_per_dollar=0.1,
                      n=2, previous_usd=10)],
        equity=1_000, peak=1_000, fraction=0.25, min_sample=5,
    )
    assert held["A"] == pytest.approx(10)
    assert held["A"] <= 10


def test_drawdown_throttle_and_ladder():
    sample = MarketSample("A", observed_per_dollar=1.0, prior_per_dollar=0.1,
                           n=10, previous_usd=1_000)
    cut = reallocate([sample], equity=95, peak=100, fraction=1.0, min_sample=5)
    assert cut["A"] == pytest.approx(47.5)
    flat = reallocate([sample], equity=90, peak=100, fraction=1.0, min_sample=5)
    assert flat["A"] == 0.0

    ladder = ScaleLadder(n_days=5)
    assert ladder.capital_usd == 500
    assert LADDER_USD == (500, 1_000, 2_500, 5_000, 10_000)
    for _ in range(4):
        ladder.record_day(reward_usd=3, markout_cost_usd=1, kill=False)
    assert ladder.capital_usd == 500
    ladder.record_day(reward_usd=1.4, markout_cost_usd=1, kill=False)
    assert ladder.good_days == 0
    for _ in range(5):
        ladder.record_day(reward_usd=3, markout_cost_usd=1, kill=False)
    assert ladder.capital_usd == 1_000
    ladder.record_day(reward_usd=3, markout_cost_usd=1, kill=True)
    assert ladder.good_days == 0
    assert ladder.capital_usd == 1_000
    book = BankrollLedger(800)
    assert capped_equity(book, ladder) == 1_000 or book.equity() == 800
    assert capped_equity(book, ladder) == 800


def test_pm_engine_place_is_paper_and_signed_reads_verify(tmp_path):
    class Spy:
        def __init__(self):
            self.called = False

        def request(self, *args, **kwargs):
            self.called = True
            raise AssertionError("read-only key must not be asked to send")

    spy = Spy()
    adapter = PMUSAdapter(transport=spy, paper=False)
    resp = adapter.engine_place(
        "btc-100k", intent="ORDER_INTENT_BUY_LONG", price_cents=49, quantity=10)
    assert resp["ok"] and resp["read_only"] and resp["paper"]
    assert resp["body"]["participateDontInitiate"] is True
    assert spy.called is False
    assert resp["order_id"].startswith("PM-PAPER-")

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from execution.polymarket_adapter import PolymarketAdapter

    seed = bytes(range(32))
    key = Ed25519PrivateKey.from_private_bytes(seed)
    pm = PolymarketAdapter(dry_run=True, db_path=str(tmp_path / "pm.db"))
    pm._client = key
    pm.api_key = "kid"
    headers = pm._signed_headers("GET", "/v1/markets", "")
    sig = base64.b64decode(headers["X-PM-Signature"])
    payload = f"{headers['X-PM-Timestamp']}GET/v1/markets".encode()
    key.public_key().verify(sig, payload)


def test_cross_venue_exposure_and_one_kill():
    kalshi = Listing("kalshi", "KXBTC-26DEC31", "Will BTC exceed 100k?", "2026-12-31")
    pm = Listing("pmus", "btc-100k", "will btc exceed 100k", "2026-12-31")
    fuzzy = Listing("pmus", "btc-100k-dec", "Will BTC exceed 100k in December", "2026-12-31")
    event_of, uncertain = match_markets([kalshi, pm, fuzzy])
    assert event_of[("kalshi", "KXBTC-26DEC31")] == event_of[("pmus", "btc-100k")]
    assert ("pmus", "btc-100k-dec") not in event_of
    assert uncertain and uncertain[0].overlap >= 0.5

    nets = net_exposure([
        Position("kalshi", "KXBTC-26DEC31", "yes", 40),
        Position("pmus", "btc-100k", "no", 40),
    ], event_of)
    assert list(nets.values()) == pytest.approx([0.0])

    reasons = limit_breaches(
        positions=[],
        event_of=event_of,
        capital_usd={"kalshi": 300, "pmus": 300},
        global_cap_usd=500,
        event_limit_usd=100,
    )
    assert reasons == ["global_capital"]

    k = KalshiAdapter(paper=True)
    placed = k.place("KXBTC-26DEC31", Side.YES, 40, 5, best_opposing_bid_cents=50)
    p = PMUSAdapter(paper=True)
    kill = CrossKill(k, p, [(placed["order_id"], "KXBTC-26DEC31")])
    kill.trip("reward_markout")
    assert kill.killed
    assert any(row["method"] == "DELETE" for row in k.sent)
    assert p.sent[-1]["path"] == "/v1/orders/open/cancel"


def test_selector_ranks_kalshi_and_pm_on_the_shared_pool():
    kalshi = KalshiMarket(
        market="KXBRENT-26OCT07", series="KXBRENT",
        period_reward_usd=50, period_seconds=86400, seconds_left=86400,
        discount_factor=0.5, target_size=100, days_to_settle=1,
        exchange_index=2, shard_cash_usd=10_000,
    )
    rich = PMQuote("btc-week", reward_pool_usd=400, n_markets=1, period_seconds=86400,
                   capital_usd=100, fill_contracts=100, fill_price_cents=50)
    shared = PMQuote("atp-day", reward_pool_usd=120, n_markets=12, period_seconds=86400,
                     capital_usd=100)
    _net, _cap, per, reward, rebate = pm_quote_economics(rich)
    assert rebate == pytest.approx(0.31)
    assert reward == pytest.approx(400)
    assert per > 1
    _n2, _c2, shared_per, shared_reward, _r2 = pm_quote_economics(shared)
    assert shared_reward == pytest.approx(10)
    assert shared_per == pytest.approx(0.1)
    ranked = rank_cross_venue([kalshi], [rich, shared])
    assert [row[1] for row in ranked] == ["btc-week", "KXBRENT-26OCT07", "atp-day"]
