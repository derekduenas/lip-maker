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
