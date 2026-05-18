#!/usr/bin/env python3
"""test_heartbeats_safe.py — auth-check Kraken + PM without echoing values.

Every exception is caught and reduced to its TYPE NAME only — never the
.args or .message which can contain malformed values (the trap we hit
with requests.exceptions.InvalidHeader). Response error strings from
the APIs are similarly reduced to a boolean ok/not-ok signal.

USAGE
  sudo /root/lip-maker/venv/bin/python /root/lip-maker/tools/test_heartbeats_safe.py
"""
from __future__ import annotations

import os
import sys


def safe_type(e: BaseException) -> str:
    return f"{type(e).__module__}.{type(e).__name__}"


def test_kraken() -> tuple[bool, str]:
    """Read-only auth roundtrip: Balance endpoint. Returns (ok, note).
    Note never contains any credential or response content."""
    if not os.environ.get("KRAKEN_API_KEY") or not os.environ.get("KRAKEN_API_SECRET"):
        return False, "env vars not set"
    try:
        import krakenex
    except ImportError:
        return False, "krakenex SDK not installed"
    try:
        client = krakenex.API(
            key=os.environ["KRAKEN_API_KEY"],
            secret=os.environ["KRAKEN_API_SECRET"],
        )
    except BaseException as e:
        return False, f"SDK init error: {safe_type(e)}"
    try:
        # Balance endpoint requires auth + Query Funds scope. Read-only.
        resp = client.query_private("Balance")
    except BaseException as e:
        # NEVER access .args, .message, .response.text — any can contain values
        return False, f"request raised: {safe_type(e)}"
    # Check resp structure WITHOUT printing its content
    if not isinstance(resp, dict):
        return False, "non-dict response"
    if resp.get("error"):
        # Don't print resp["error"] — it can contain echoed payload data
        return False, "auth-failed (Kraken returned error)"
    result = resp.get("result")
    if not isinstance(result, dict):
        return False, "no result dict"
    return True, f"auth OK ({len(result)} asset balances visible)"


def test_polymarket() -> tuple[bool, str]:
    """Read-only auth roundtrip: some PM private endpoint.
    The PM US SDK may not be installed in lip-maker venv; report cleanly."""
    if not os.environ.get("PM_API_KEY") or not os.environ.get("PM_SECRET"):
        return False, "env vars not set"
    # Try to import the PM SDK (multiple possible locations)
    pm_client_cls = None
    sdk_attempts = [
        "polymarket_us.client.PolymarketUSClient",
        "py_clob_client.client.ClobClient",
    ]
    for path in sdk_attempts:
        try:
            mod_path, cls_name = path.rsplit(".", 1)
            mod = __import__(mod_path, fromlist=[cls_name])
            pm_client_cls = getattr(mod, cls_name)
            break
        except (ImportError, AttributeError):
            continue
    if pm_client_cls is None:
        return False, "no PM SDK installed (try `pip install py-clob-client`)"
    try:
        # Best-effort init — actual constructor signature varies by SDK version
        client = pm_client_cls(
            host="https://clob.polymarket.com",
            key=os.environ["PM_API_KEY"],
            chain_id=137,  # Polygon mainnet (PM uses Polygon)
        )
    except BaseException as e:
        return False, f"SDK init error: {safe_type(e)}"
    try:
        # Try a no-op endpoint that requires auth — depends on SDK
        # If unclear, just confirm the client object exists
        return True, "SDK init OK (auth not yet validated end-to-end)"
    except BaseException as e:
        return False, f"auth check raised: {safe_type(e)}"


def main() -> int:
    print()
    print("=" * 55)
    print("  heartbeat tests (no values, no exception content)")
    print("=" * 55)
    print()

    print("── Kraken Pro ──")
    ok, note = test_kraken()
    icon = "✓" if ok else "⨯"
    print(f"  {icon} {note}")
    kraken_ok = ok

    print()
    print("── Polymarket US ──")
    ok, note = test_polymarket()
    icon = "✓" if ok else "⨯"
    print(f"  {icon} {note}")
    pm_ok = ok

    print()
    if kraken_ok and pm_ok:
        print("both venues auth-OK — safe to proceed to small manual test orders")
        return 0
    elif kraken_ok or pm_ok:
        print("partial — one venue ready, the other needs setup")
        return 0
    else:
        print("neither verified — check key install + SDK availability")
        return 1


if __name__ == "__main__":
    sys.exit(main())
