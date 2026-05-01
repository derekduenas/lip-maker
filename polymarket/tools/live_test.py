"""Polymarket live test — place ONE small order with safety prompts.

HARD CAPS (cannot be overridden via flags):
  - $20 max notional per order
  - Single market only
  - Requires interactive Y/N confirmation before send

PROCESS:
  1. Pull current BBO + book for chosen market
  2. Compute: side, price, qty, max notional
  3. PRINT THE TRADE
  4. Prompt: type "YES TRADE" to confirm
  5. POST /v1/orders via SDK orders.create()
  6. Save order_id to logs/live_orders.log
  7. Tell user how to monitor

USAGE:
  # Auto-pick top weather market, BUY YES at best bid:
  python tools/live_test.py

  # Specific market + side:
  python tools/live_test.py --slug 'tc-temp-laxhigh-2026-04-29-gte65f' --side yes

  # Smaller order:
  python tools/live_test.py --max-notional 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS

# ── HARD CAPS — cannot be overridden ─────────────────────────────────────
HARD_CAP_NOTIONAL_USD = 20.0
HARD_CAP_QUANTITY     = 500
LOG_FILE = PROJECT_ROOT / "logs" / "live_orders.log"


def get_client() -> PolymarketUS:
    return PolymarketUS(
        key_id=os.getenv("PM_API_KEY_ID"),
        secret_key=(PROJECT_ROOT / "config" / "polymarket_secret_key.b64").read_text().strip(),
    )


def pick_top_weather_market(client: PolymarketUS) -> str | None:
    """Default target: a qualified weather market with low top-of-book."""
    try:
        r = client.search.query({"q": "weather", "limit": 30})
        events = r.get("events", []) if isinstance(r, dict) else []
        for e in events:
            for m in (e.get("markets", []) if isinstance(e, dict) else []):
                slug = m.get("slug", "")
                if slug.startswith("tc-temp-") and not e.get("closed"):
                    return slug
    except Exception:
        pass
    return None


def fetch_bbo(client: PolymarketUS, slug: str) -> dict | None:
    try:
        r = client.markets.bbo(slug)
    except Exception as e:
        print(f"  🔴 BBO fetch failed: {e}")
        return None
    md = r.get("marketData", r) if isinstance(r, dict) else r
    def _px(field):
        v = md.get(field) if isinstance(md, dict) else None
        if v is None: return None
        if isinstance(v, dict): return float(v.get("value", 0))
        return float(v)
    return {
        "yes_bid":  _px("bestBid"),
        "yes_ask":  _px("bestAsk"),
        "current":  _px("currentPx"),
    }


def confirm(prompt: str, expected: str) -> bool:
    """User must type the exact expected string."""
    actual = input(f"{prompt}\n  > ").strip()
    return actual == expected


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", default=None, help="Market slug (default: auto-pick weather)")
    p.add_argument("--side", choices=["yes", "no"], default="yes",
                   help="YES = buy long, NO = buy short")
    p.add_argument("--max-notional", type=float, default=10.0,
                   help=f"Max USD per order (capped at ${HARD_CAP_NOTIONAL_USD})")
    p.add_argument("--quantity", type=int, default=None,
                   help="Override calculated qty (capped at HARD_CAP_QUANTITY)")
    a = p.parse_args()

    # Hard cap enforcement
    notional_cap = min(a.max_notional, HARD_CAP_NOTIONAL_USD)

    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  POLYMARKET LIVE TEST")
    print(f"  HARD CAPS: ${HARD_CAP_NOTIONAL_USD} notional, {HARD_CAP_QUANTITY} qty")
    print(f"══════════════════════════════════════════════════════════════\n")

    client = get_client()

    # Account check
    print("  account check...")
    try:
        bal = client.account.balances()
        b = bal.get("balances", [{}])[0]
        balance_usd = float(b.get("currentBalance", 0))
        buying_power = float(b.get("buyingPower", 0))
        print(f"    balance=${balance_usd:.2f}  buying_power=${buying_power:.2f}")
    except Exception as e:
        print(f"  🔴 account check failed: {e}")
        return 1

    if buying_power < notional_cap:
        print(f"  🔴 buying power ${buying_power:.2f} < notional ${notional_cap:.2f} — abort")
        return 1

    # Pick market
    slug = a.slug or pick_top_weather_market(client)
    if not slug:
        print("  🔴 no market specified and auto-pick failed")
        return 1
    print(f"\n  market: {slug}")

    # Get BBO
    bbo = fetch_bbo(client, slug)
    if not bbo or not bbo["yes_bid"] or not bbo["yes_ask"]:
        print(f"  🔴 no BBO available for {slug}")
        return 1
    print(f"    bbo: yes_bid={bbo['yes_bid']} yes_ask={bbo['yes_ask']} current={bbo['current']}")

    # Build order
    if a.side == "yes":
        intent = "ORDER_INTENT_BUY_LONG"
        price = bbo["yes_bid"]    # post at-best to be a maker (or 1 tick inside)
        side_label = "YES (long)"
    else:
        intent = "ORDER_INTENT_BUY_SHORT"
        price = round(1.0 - bbo["yes_ask"], 3)
        side_label = "NO (short)"

    if price <= 0 or price >= 1:
        print(f"  🔴 invalid price {price}")
        return 1

    # Compute qty respecting notional cap
    max_qty_by_notional = int(notional_cap / price)
    qty = min(
        a.quantity if a.quantity else max_qty_by_notional,
        max_qty_by_notional,
        HARD_CAP_QUANTITY,
    )
    if qty < 5:
        print(f"  🔴 qty {qty} < min sensible (5) — try bigger --max-notional")
        return 1
    actual_notional = qty * price

    # Print full plan
    print(f"\n  ┌─────────────────────────────────────────")
    print(f"  │  INTENDED LIVE ORDER")
    print(f"  ├─────────────────────────────────────────")
    print(f"  │  market:     {slug}")
    print(f"  │  side:       {side_label}")
    print(f"  │  price:      ${price:.3f}")
    print(f"  │  quantity:   {qty} contracts")
    print(f"  │  notional:   ${actual_notional:.2f}")
    print(f"  │  max loss:   ${actual_notional:.2f} (settles wrong)")
    print(f"  │  max gain:   ${qty * (1 - price):.2f} (settles right) + LIP rebate")
    print(f"  │  tif:        GOOD_TILL_CANCEL")
    print(f"  └─────────────────────────────────────────")

    # Server-side preview first
    order = {
        "marketSlug": slug,
        "intent":     intent,
        "type":       "ORDER_TYPE_LIMIT",
        "price":      {"value": f"{price:.3f}", "currency": "USD"},
        "quantity":   qty,
        "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    }
    print(f"\n  validating via /v1/order/preview...")
    try:
        preview = client.orders.preview({"request": order})
        po = preview.get("order", {}) if isinstance(preview, dict) else {}
        commission = po.get("commissionNotionalTotalCollected", {}).get("value", "?")
        print(f"    ✓ preview OK  commission=${commission}  state={po.get('state')}")
    except Exception as e:
        print(f"  🔴 preview failed: {type(e).__name__}: {str(e)[:200]}")
        return 1

    # Confirmation gate
    print(f"\n  ⚠️  This will POST a REAL order using your $270 buying power.")
    print(f"     Type 'YES TRADE' (exact) to confirm, anything else cancels.")
    if not confirm("", "YES TRADE"):
        print("  ✗ cancelled")
        return 0

    # Send
    print(f"\n  sending /v1/orders...")
    try:
        resp = client.orders.create(order)
    except Exception as e:
        print(f"  🔴 order failed: {type(e).__name__}: {str(e)[:300]}")
        return 1

    # Log
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_entry = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "request":  order,
        "response": resp,
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(log_entry, default=str) + "\n")

    # Extract order_id
    o = resp.get("order", {}) if isinstance(resp, dict) else {}
    order_id = o.get("id", "?")
    state = o.get("state", "?")

    print(f"\n  ✓ ORDER PLACED")
    print(f"    order_id: {order_id}")
    print(f"    state:    {state}")
    print(f"    logged to: {LOG_FILE}")
    print(f"\n  next: monitor with `python tools/watch_order.py {order_id}`")
    print(f"        or check polymarket.us UI under Open Orders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
