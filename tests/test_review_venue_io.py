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
