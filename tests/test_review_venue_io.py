"""Review fixes (venue area): venue I/O parsing, env loading, paper fills,
read-only allowlists, tick-size exclusion, calibration."""
from decimal import Decimal

import pytest


# ------------------------------------------------------------------ item 10
def test_to_cents_branches_on_type_not_magnitude():
    from mm.venues.kalshi import _to_cents, resting_quote
    assert _to_cents(1) == 1            # legacy int cents: 1c, not $1
    assert _to_cents(45) == 45
    assert _to_cents("45") == 45
    assert _to_cents("0.45") == 45
    assert _to_cents("0.0100") == 1
    assert _to_cents(Decimal("0.45")) == 45
    assert _to_cents(0.45) == 45
    assert _to_cents("1.0000") == 100
    for bad in (True, "45.5", 45.5, "abc"):
        with pytest.raises((ValueError, TypeError)):
            _to_cents(bad)
    assert resting_quote({"book_side": "bid", "yes_price": 1}) == ("yes", 1)
    assert resting_quote({"book_side": "ask", "yes_price": 1}) == ("no", 99)
    assert resting_quote({"book_side": "bid", "yes_price_dollars": "0.0100"}) == ("yes", 1)


# ------------------------------------------------------------------ item 11
def test_repo_dotenv_is_opt_in_and_never_sets_lip_keys(tmp_path):
    from execution import kalshi_auth as A
    env_file = tmp_path / ".env"
    env_file.write_text("LIP_DEMO=1\nLIP_LIVE_ACK=yes\nLIP_BANKROLL=99999\nKALSHI_KEY_ID=abc\n"
                        "export LIP_PAPER=false\n")
    env: dict = {}
    assert A.maybe_load_repo_dotenv(str(env_file), environ=env) is False and env == {}
    env = {"LIP_LOAD_DOTENV": "1"}
    assert A.maybe_load_repo_dotenv(str(env_file), environ=env) is True
    assert env == {"LIP_LOAD_DOTENV": "1", "KALSHI_KEY_ID": "abc"}
    # the low-level loader refuses LIP_* keys too, and never overrides
    env = {"KALSHI_KEY_ID": "keep"}
    A._load_dotenv_simple(str(env_file), environ=env)
    assert env == {"KALSHI_KEY_ID": "keep"}


def test_importing_kalshi_modules_does_not_touch_environ(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    code = ("import os, json; before = dict(os.environ); "
            "import execution.kalshi_auth, execution.kalshi_ws; "
            "print(json.dumps(sorted(set(os.environ) - set(before))))")
    env = {k: v for k, v in __import__("os").environ.items() if k != "LIP_LOAD_DOTENV"}
    out = subprocess.run([sys.executable, "-c", code], cwd=str(repo), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]"


# ------------------------------------------------------------------ item 12
def _sim_with_yes_bid(price=41, size=10, ticker="KXA-1"):
    from engine.lip_scorer import BookLevel, BookState
    from execution.paper_fills import PaperFillSimulator
    sim = PaperFillSimulator(latency_ms=0)
    bk = BookState(market_ticker=ticker, yes_bids=[BookLevel(40, 5.0)], no_bids=[BookLevel(55, 5.0)])
    sim.track(order_id="o", market_ticker=ticker, side="yes", price_cents=price, size=size, book=bk, now=0.0)
    return sim


def test_paper_fills_accept_legacy_integer_cent_prices():
    sim = _sim_with_yes_bid()
    fills = sim.apply_trades([{"trade_id": "t1", "ticker": "KXA-1", "count": 3, "yes_price": 41,
                               "no_price": 59, "taker_side": "no", "created_time": "2026-10-01T00:00:01Z"}])
    assert [(f["count"], f["price_cents"]) for f in fills] == [(3.0, 41)]
    # yes_price only (no NO price): NO side derived as 100 - yes
    sim = _sim_with_yes_bid()
    fills = sim.apply_trades([{"trade_id": "t2", "ticker": "KXA-1", "count": 2, "yes_price": 40,
                               "taker_side": "no", "created_time": "2026-10-01T00:00:01Z"}])
    assert [f["count"] for f in fills] == [2.0]


def test_paper_fills_seen_trades_is_bounded():
    from execution import paper_fills as PF
    sim = PF.PaperFillSimulator(latency_ms=0, seen_trades_max=50)
    trades = [{"trade_id": f"t{i}", "ticker": "KXZ-1", "count": 1, "yes_price_dollars": "0.40",
               "no_price_dollars": "0.60", "taker_side": "no", "created_time": "2026-10-01T00:00:01Z"}
              for i in range(500)]
    sim.apply_trades(trades)
    assert len(sim._seen_trades) == 50 and sim.trades_observed == 500
    assert "t499" in sim._seen_trades and "t0" not in sim._seen_trades
    sim.apply_trades(trades[-10:])  # recent ids still de-duplicated
    assert sim.trades_observed == 500
    assert PF.PaperFillSimulator().seen_trades_max == PF.SEEN_TRADES_MAX


# ------------------------------------------------------------------ item 13
@pytest.mark.parametrize("path", [
    "/markets/../portfolio/balance", "/markets/..", "/series/./x", "/markets/%2e%2e/orders",
    "/markets/KX%2FA", "/markets/KX%2fA/orderbook", "/markets/KX%5CA", "/markets/KX\\A",
    "/events/%2E%2E", "/markets/KX%252FA", "/trade-api/v2/markets/../exchange/status",
])
def test_readonly_allowlist_rejects_traversal(path):
    from mm.venues.readonly import get_allowed
    assert get_allowed(path) is False


def test_readonly_allowlist_still_accepts_public_routes():
    from mm.venues.readonly import get_allowed
    for path in ("/markets", "/markets/KXA-26DEC-T1", "/markets/KXA-26DEC-T1/orderbook",
                 "/series/KXA", "/events/KXA-26DEC", "/incentive_programs", "/exchange/status",
                 "/markets?tickers=A,B&limit=1000", "/trade-api/v2/markets/trades"):
        assert get_allowed(path) is True, path


@pytest.mark.parametrize("path", [
    "/v1/markets/../book", "/v1/markets/./book", "/v1/market/slug/..",
    "/v1/markets/a%2Fb/book", "/v1/markets/a\\b/book",
])
def test_pmus_allowlist_rejects_traversal(path):
    from mm.unattended import pmus_paper as P
    with pytest.raises(P.PMUSOrderBlocked):
        P.check_request("GET", path)


def test_pmus_allowlist_keeps_encoded_page_tokens():
    from mm.unattended import pmus_paper as P
    P.check_request("GET", "/v1/incentives?page_size=100&statuses=active&page_token=ab%2Fcd%3D%3D")
    P.check_request("GET", "/v1/markets/rtc-x-2026-10-01-abc/book")


def test_screen_skips_series_names_the_reader_would_refuse():
    from mm.unattended.screen import MetaCache
    calls = []

    class _Reader:
        def get(self, path, params=None):
            calls.append(path)
            return {"series": {"category": "Economics", "fee_type": "quadratic"}}
    c = MetaCache(path="/nonexistent/x.json", sleep=lambda s: None)
    assert c.fetch_series(_Reader(), ["..", "a/b", "KXOK"]) == 1
    assert calls == ["/series/KXOK"] and c.failures == 2


# ------------------------------------------------------------------ item 14
def _row(**kw):
    row = {"ticker": "KXA-26DEC-T1", "close_time": "2026-12-01T00:00:00Z", "exchange_index": 0,
           "status": "active", "event_ticker": "KXA-26DEC"}
    row.update(kw)
    return row


@pytest.mark.parametrize("extra,ok", [
    ({}, True),                                                          # legacy row
    ({"price_level_structure": "linear_cent"}, True),
    ({"price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]}, True),
    ({"price_level_structure": "deci_cent"}, False),
    ({"price_level_structure": "center_deci_edge_centi_cent"}, False),
    ({"price_level_structure": "linear_cent",                            # ranges win
      "price_ranges": [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                       {"start": "0.1000", "end": "1.0000", "step": "0.0100"}]}, False),
    ({"tick_size": 1}, True),
    ({"tick_size": 5}, False),
])
def test_market_meta_flags_non_cent_tick(extra, ok):
    from mm.unattended.screen import market_meta
    assert market_meta(_row(**extra), now=0.0)["tick_1c"] is ok


def test_screen_excludes_subcent_markets(monkeypatch):
    from mm.unattended import screen as S
    monkeypatch.setenv("LIP_LONG_DATED_ANY_DAYS", "1e9")
    monkeypatch.setenv("LIP_LONG_DATED_EVENT_DAYS", "1e9")
    frame = {"market": "KXA-26DEC-T1", "series": "KXA", "program_id": "p", "period_reward_usd": 50,
             "period_seconds": 86400, "end_ts": 2e12, "discount_factor": 0.5, "target_size": 100}
    for structure, n in (("linear_cent", 1), ("deci_cent", 0)):
        c = S.MetaCache(path="/nonexistent/x.json")
        c.markets["KXA-26DEC-T1"] = S.market_meta(_row(price_level_structure=structure,
                                                       close_time="2027-01-01T00:00:00Z"), now=0.0)
        c.series["KXA"] = {"category": "Economics", "tags": [], "fee_type": "quadratic", "fetched": 0}
        out, stats = S.screen([frame], c, now=1.79e9)
        assert len(out) == n, stats
        if not n:
            assert stats["reasons"] == {"subcent_tick": 1}


# ------------------------------------------------------------------ item 15
def test_series_factor_is_clamped_ratio_of_sums_of_statement_rows():
    from engine.lip_calibration import RatioObs, series_factors
    D = Decimal
    # proofs/engine/t5_calib.py: one tiny estimate used to dominate the mean of ratios
    obs = [RatioObs("KXA", D("1.00"), D("12.00")), RatioObs("KXA", D("40.00"), D("30.00"))]
    f = series_factors(obs, strength=5)
    assert f["KXA"] == pytest.approx((2 * (42 / 41) + 5 * 1.0) / 7)
    # clamped to [0, 2]
    f = series_factors([RatioObs("KXB", D("10"), D("50"))], strength=5)
    assert f["KXB"] == pytest.approx((1 * 2.0 + 5 * 1.0) / 6)
    f = series_factors([RatioObs("KXC", D("10"), D("-5"))], strength=0)
    assert f["KXC"] == 0.0
    # inferred (balance-residual) rows carry no per-series information
    inferred = [RatioObs("KXD", D("10"), D("30"), inferred=True)]
    assert series_factors(inferred) == {}
    mixed = series_factors(obs + [RatioObs("KXA", D("1"), D("100"), inferred=True)], strength=5)
    assert mixed["KXA"] == pytest.approx((2 * (42 / 41) + 5) / 7)


def test_inferred_credits_say_they_are_not_calibration_input():
    from engine.lip_reconcile import infer_reward_credits
    out = infer_reward_credits(balance_delta_usd=50, shares={"KXA-1": Decimal("10"), "KXB-1": Decimal("30")})
    assert out["credits"] and all(c["calibration_eligible"] is False for c in out["credits"])
    assert out["calibration_eligible"] is False
