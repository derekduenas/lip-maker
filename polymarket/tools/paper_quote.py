"""Polymarket paper-mode quote — uses SDK orders.preview (server validates).

This is REAL paper mode: orders.preview hits Polymarket's server which
validates the order shape, checks pricing tier, computes expected cost,
returns success/failure. NO MONEY MOVES, NO ORDER PLACED.

For each top-N market we'd quote:
  1. Pull current BBO via SDK markets.bbo(slug)
  2. Build at-best two-sided quote
  3. Call orders.preview() for each side
  4. Print result + projected daily payout

USAGE:
  python tools/paper_quote.py --top 5 --size 100
  python tools/paper_quote.py --slug btc-100k-2025  # specific market
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS


def get_client() -> PolymarketUS:
    key_id = os.getenv("PM_API_KEY_ID")
    secret_path = PROJECT_ROOT / "config" / "polymarket_secret_key.b64"
    return PolymarketUS(key_id=key_id, secret_key=secret_path.read_text().strip())


def preview_quote(client, slug: str, size: int, verbose: bool = True) -> dict:
    """Pull BBO, build at-best two-sided, preview both legs."""
    out = {"slug": slug, "yes": None, "no": None, "bbo": None}
    # 1. Get BBO
    try:
        bbo = client.markets.bbo(slug)
        out["bbo"] = bbo
    except Exception as e:
        out["error"] = f"bbo failed: {e}"
        return out

    # Extract best yes bid + ask. SDK shape:
    #   bbo = {"marketData": {"bestBid": {"value": "0.41", ...}, "bestAsk": {...}, ...}}
    md = bbo.get("marketData", bbo) if isinstance(bbo, dict) else bbo
    def _price(field) -> float | None:
        v = md.get(field) if isinstance(md, dict) else getattr(md, field, None)
        if v is None: return None
        if isinstance(v, dict):
            return float(v.get("value", 0))
        return float(v)
    yes_bid = _price("bestBid")
    yes_ask = _price("bestAsk")
    if yes_bid is None or yes_ask is None:
        out["error"] = f"no BBO data: {bbo}"
        return out
    out["best_yes_bid"] = yes_bid
    out["best_yes_ask"] = yes_ask
    out["midpoint"] = (yes_bid + yes_ask) / 2.0

    # 2. Build YES side preview (buy yes at best bid)
    try:
        yes_req = {
            "marketSlug": slug,
            "intent":     "ORDER_INTENT_BUY_LONG",
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": f"{yes_bid:.3f}", "currency": "USD"},
            "quantity":   size,
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        out["yes_request"] = yes_req
        out["yes"] = client.orders.preview({"request": yes_req})
    except Exception as e:
        out["yes_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    # 3. Build NO side preview (buy no at implied no-bid = 1 - yes_ask)
    try:
        no_price = round(1.0 - yes_ask, 3)
        no_req = {
            "marketSlug": slug,
            "intent":     "ORDER_INTENT_BUY_SHORT",
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": f"{no_price:.3f}", "currency": "USD"},
            "quantity":   size,
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        out["no_request"] = no_req
        out["no"] = client.orders.preview({"request": no_req})
    except Exception as e:
        out["no_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", default=None,
                   help="Specific market slug to preview")
    p.add_argument("--top", type=int, default=5,
                   help="If no --slug: preview top N markets from list")
    p.add_argument("--size", type=int, default=100,
                   help="Order size in contracts (default 100)")
    a = p.parse_args()

    c = get_client()

    if a.slug:
        slugs = [a.slug]
    else:
        try:
            m = c.markets.list({"limit": a.top})
            markets = m.get("markets", []) if isinstance(m, dict) else m
            slugs = [(mk.get("slug") if isinstance(mk, dict) else getattr(mk, 'slug', None))
                     for mk in markets][:a.top]
            slugs = [s for s in slugs if s]
        except Exception as e:
            print(f"ERROR: markets.list failed: {e}")
            return 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  PAPER QUOTE PREVIEW — {now}  size={a.size}")
    print(f"══════════════════════════════════════════════════════════════\n")

    for slug in slugs:
        print(f"\n📍 {slug}")
        r = preview_quote(c, slug, a.size)
        if "error" in r:
            print(f"  🔴 {r['error']}")
            continue
        print(f"  best yes bid={r.get('best_yes_bid')} ask={r.get('best_yes_ask')} "
              f"midpoint={r.get('midpoint'):.3f}")
        if r.get("yes"):
            print(f"  ✓ YES preview: {r['yes']}")
        elif r.get("yes_error"):
            print(f"  🔴 YES: {r['yes_error']}")
        if r.get("no"):
            print(f"  ✓ NO preview: {r['no']}")
        elif r.get("no_error"):
            print(f"  🔴 NO: {r['no_error']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
