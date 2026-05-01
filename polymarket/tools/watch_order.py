"""Polymarket order monitor — poll status + book position.

Watches a specific order placed by live_test.py. Prints state changes,
fill events, and current book context (are we still at-best?).

USAGE:
  python tools/watch_order.py <order_id>
  python tools/watch_order.py <order_id> --interval 15
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS


def get_client() -> PolymarketUS:
    return PolymarketUS(
        key_id=os.getenv("PM_API_KEY_ID"),
        secret_key=(PROJECT_ROOT / "config" / "polymarket_secret_key.b64").read_text().strip(),
    )


def fetch_order_state(client: PolymarketUS, order_id: str) -> dict | None:
    try:
        return client.orders.retrieve(order_id)
    except Exception as e:
        # Order may not be in /open list anymore (filled/cancelled)
        return {"error": str(e)[:120]}


def fetch_bbo(client: PolymarketUS, slug: str) -> dict | None:
    try:
        r = client.markets.bbo(slug)
        md = r.get("marketData", r) if isinstance(r, dict) else r
        def _px(field):
            v = md.get(field) if isinstance(md, dict) else None
            if isinstance(v, dict): return float(v.get("value", 0))
            return float(v) if v is not None else None
        return {
            "yes_bid":  _px("bestBid"),
            "yes_ask":  _px("bestAsk"),
            "current":  _px("currentPx"),
        }
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("order_id", help="Order ID returned by live_test.py")
    p.add_argument("--interval", type=int, default=30, help="Poll seconds (default 30)")
    p.add_argument("--once", action="store_true", help="Single status check, no loop")
    a = p.parse_args()

    client = get_client()
    last_state = None
    last_filled = 0
    cycle = 0

    print(f"\n  watching order {a.order_id}  (interval {a.interval}s, Ctrl-C to stop)\n")

    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")

        # Order status
        os_data = fetch_order_state(client, a.order_id)
        if not os_data:
            print(f"  {now}  · order fetch returned None")
        elif "error" in os_data:
            # Try open orders list as fallback
            try:
                open_orders = client.orders.list({"limit": 200}).get("orders", [])
                match = next((o for o in open_orders if o.get("id") == a.order_id), None)
                if match:
                    os_data = {"order": match}
                else:
                    os_data = {"order": {"state": "NOT_OPEN", "note": os_data["error"]}}
            except Exception:
                pass

        order = os_data.get("order", {}) if isinstance(os_data, dict) else {}
        state = order.get("state", "?")
        cum = int(float(order.get("cumQuantity", 0)))
        leaves = int(float(order.get("leavesQuantity", 0)))
        slug = order.get("marketSlug", "?")
        my_price = order.get("price", {}).get("value", "?") if isinstance(order.get("price"), dict) else "?"

        # Fetch live BBO for book context
        bbo = fetch_bbo(client, slug) if slug != "?" else None
        if bbo and bbo.get("yes_bid"):
            at_best = "✓ at-best" if abs(float(my_price) - bbo["yes_bid"]) < 0.001 or abs(float(my_price) - (1-bbo["yes_ask"])) < 0.001 else "⚠ off-best"
            book_str = f"yes_bid={bbo['yes_bid']:.3f} yes_ask={bbo['yes_ask']:.3f}"
        else:
            at_best = "?"
            book_str = "(no bbo)"

        # Print row
        change_marker = ""
        if state != last_state:
            change_marker = "  ← state changed"
            last_state = state
        if cum > last_filled:
            change_marker += f"  ← +{cum-last_filled} filled!"
            last_filled = cum

        print(f"  {now}  state={state:<25s}  filled={cum}/{cum+leaves}  "
              f"@${my_price}  {at_best}  {book_str}{change_marker}")

        if a.once:
            return 0
        if state in ("ORDER_STATE_FILLED", "ORDER_STATE_CANCELLED",
                     "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED"):
            print(f"\n  terminal state reached → stopping monitor")
            return 0

        try:
            time.sleep(a.interval)
        except KeyboardInterrupt:
            print(f"\n  ✗ user interrupted — order may still be live")
            print(f"  to cancel: python -c \"import sys; sys.path.insert(0,'.'); "
                  f"from tools.live_test import get_client; get_client().orders.cancel("
                  f"'{a.order_id}', {{}})\"")
            return 0


if __name__ == "__main__":
    sys.exit(main())
