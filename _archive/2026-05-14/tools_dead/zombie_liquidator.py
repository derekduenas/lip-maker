"""ZOMBIE LIQUIDATOR — closes positions in markets with NO active LIP program.

THE PROBLEM:
  AEGIS dashboard shows: 553 markets enrolled → 40 ranked → 1 quoting.
  Why so few quoting? Because $1,296 of capital is tied up in 70 open
  positions, hitting safety caps. AEGIS can't ATTACK new high-yield LIP
  enrollments.

  Audit (2026-05-06): 27 of 70 positions are ZOMBIES — markets where the
  LIP program has expired/paid_out, so they earn $0 rebate. They tie up
  $313 capital we could redeploy at ~$2/day × 5-15 markets = real $.

  Existing stranded_liquidator skips these because they're "too_far_out"
  (settle hundreds of hours away). It doesn't know about LIP expiry.

THIS TOOL:
  1. Pulls live positions from Kalshi
  2. Cross-references with lip_programs (active = end_date > now AND not paid_out)
  3. For positions NOT in active LIP: post a SELL at best opposing bid
     (we long YES → sell yes at yes_bid; we long NO → sell no at no_bid)
  4. If no bid: skip (would have to take a 50¢+ loss)
  5. Log everything, optionally execute (--live)

USAGE:
  python3 zombie_liquidator.py            # dry-run audit (default — safe)
  python3 zombie_liquidator.py --live     # actually post liquidation orders
  python3 zombie_liquidator.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

LIP_PATH = Path("/root/lip-maker")
LIP_DB = LIP_PATH / "data" / "lip_maker.db"
MIN_BID_TO_LIQUIDATE_CENTS = 1   # if best bid < 1c, position is worthless to sell


def _kalshi_client():
    cwd = os.getcwd()
    os.chdir(str(LIP_PATH))
    sys.path.insert(0, str(LIP_PATH))
    from execution.kalshi_auth import KalshiClient
    c = KalshiClient()
    os.chdir(cwd)
    return c


def _all_positions(c):
    out = []
    cursor = None
    while True:
        p = {"limit": 200}
        if cursor:
            p["cursor"] = cursor
        r = c.get("/portfolio/positions", params=p)
        out.extend(r.get("market_positions") or [])
        cursor = r.get("cursor")
        if not cursor:
            break
    return out


def _active_lip_tickers():
    conn = sqlite3.connect(str(LIP_DB), timeout=10)
    rows = conn.execute(
        "SELECT market_ticker FROM lip_programs "
        "WHERE end_date > date('now') AND paid_out=0"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def _orderbook(c, ticker):
    """Returns dict with yes_bid/ask, no_bid/ask in cents + bid sizes.
    Kalshi /markets returns *_dollars fields as strings (e.g. '0.1800')."""
    try:
        r = c.get("/markets", params={"tickers": ticker})
        m = (r.get("markets") or [None])[0]
        if not m:
            return None
        def to_cents(v):
            if v is None: return None
            try: return round(float(v) * 100)
            except (ValueError, TypeError): return None
        return {
            "yes_bid":  to_cents(m.get("yes_bid_dollars")),
            "yes_ask":  to_cents(m.get("yes_ask_dollars")),
            "no_bid":   to_cents(m.get("no_bid_dollars")),
            "no_ask":   to_cents(m.get("no_ask_dollars")),
            "yes_bid_size": int(float(m.get("yes_bid_size_fp") or 0)),
            "yes_ask_size": int(float(m.get("yes_ask_size_fp") or 0)),
        }
    except Exception:
        return None


def _post_liquidate(c, ticker, side, qty, price_cents):
    """Post a SELL/CLOSE order at the BID (taker, accepts the bid liquidity).
    Drops post_only since selling at bid IS crossing. Maker fee 1.75% × p(1-p)
    becomes taker fee 7% × p(1-p) — for typical 25¢ price, ~1¢/contract extra.
    Worth it to free zombie capital that earns $0/day."""
    body = {
        "ticker":          ticker,
        "client_order_id": str(uuid.uuid4()),
        "side":            side,
        "action":          "sell",
        "type":            "limit",
        "count":           int(abs(qty)),
    }
    if side == "yes":
        body["yes_price"] = int(price_cents)
    else:
        body["no_price"] = int(price_cents)
    return c.post("/portfolio/orders", body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="actually post liquidation orders (default: dry-run)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    c = _kalshi_client()
    positions = _all_positions(c)
    active_lip = _active_lip_tickers()

    active = [p for p in positions if float(p.get("position_fp", 0)) != 0]
    zombies = [p for p in active if p["ticker"] not in active_lip]

    actions = []
    skipped = []
    for z in zombies:
        t = z["ticker"]
        pos = float(z["position_fp"])
        exposure = float(z["market_exposure_dollars"])
        side_held = "yes" if pos > 0 else "no"      # what we sell
        full_qty = abs(int(pos))
        ob = _orderbook(c, t)
        if not ob:
            skipped.append({"ticker": t, "pos": pos, "exposure": exposure,
                            "reason": "no_orderbook"})
            continue
        # Sell our YES at the opposing NO_ask reciprocal = 100 - no_ask. Or
        # equivalently sell yes at yes_bid. Same price, different mental model.
        # CRITICAL: cap qty to opposing-side liquidity so post_only doesn't reject.
        # When we sell YES, we hit YES BIDS — those bids are sized by yes_bid_size.
        # But Kalshi packs both sides into one book; safest to use small chunks.
        if side_held == "yes":
            sell_price = ob["yes_bid"]
            # Available depth at top of YES bid book
            depth = ob.get("yes_bid_size") or 0
        else:
            sell_price = ob["no_bid"]
            # NO_bid_size isn't in the API response — use yes_ask_size as proxy
            # (selling NO = same as buying YES at no_bid_price = symmetric depth)
            depth = ob.get("yes_ask_size") or 0
        if sell_price is None or sell_price < MIN_BID_TO_LIQUIDATE_CENTS:
            skipped.append({"ticker": t, "pos": pos, "exposure": exposure,
                            "reason": f"no_bid_{sell_price}c"})
            continue
        # Cap qty to top-of-book depth (post_only requires it fit at the price)
        qty = min(full_qty, depth) if depth > 0 else full_qty
        if qty == 0:
            skipped.append({"ticker": t, "pos": pos, "exposure": exposure,
                            "reason": f"zero_depth (full_qty={full_qty}, depth={depth})"})
            continue
        plan = {
            "ticker":   t,
            "side":     side_held,
            "qty":      qty,
            "full_qty": full_qty,
            "depth":    depth,
            "sell_at":  sell_price,
            "exposure": round(exposure, 2),
            "expected_recovery": round(sell_price * qty / 100.0, 2),
            "expected_loss":     round((exposure / max(1, full_qty)) * qty - (sell_price * qty / 100.0), 2),
        }
        if a.live:
            try:
                resp = _post_liquidate(c, t, side_held, qty, sell_price)
                plan["order_id"] = (resp or {}).get("order", {}).get("order_id", "?")
                plan["status"] = "POSTED"
            except Exception as e:
                plan["status"] = f"ERR:{str(e)[:50]}"
        else:
            plan["status"] = "DRY_RUN"
        actions.append(plan)

    summary = {
        "ts":             datetime.now(timezone.utc).isoformat(),
        "n_positions":    len(active),
        "n_zombies":      len(zombies),
        "n_actionable":   len(actions),
        "n_skipped":      len(skipped),
        "capital_to_free": sum(a["expected_recovery"] for a in actions),
        "expected_loss":   sum(a["expected_loss"] for a in actions),
        "live":           a.live,
        "actions":        actions,
        "skipped":        skipped,
    }

    if a.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    mode = "🟢 LIVE" if a.live else "🟡 DRY-RUN"
    print()
    print("═" * 75)
    print(f"  ZOMBIE LIQUIDATOR — {mode}")
    print("═" * 75)
    print(f"  Positions:        {len(active)}")
    print(f"  Zombies:          {len(zombies)}  (no active LIP)")
    print(f"  Actionable:       {len(actions)}  (have a bid to sell into)")
    print(f"  Skipped:          {len(skipped)}  (no bid)")
    print(f"  Capital to free:  ${summary['capital_to_free']:.2f}")
    print(f"  Expected loss:    ${summary['expected_loss']:.2f}  (vs $0/day rebate)")
    print()
    if actions:
        print(f"  {'TICKER':<38} {'SIDE':<4} {'QTY':>5} {'SELL@':>5} {'EXPOSE':>8} {'RECOVER':>8} {'STATUS':>10}")
        for x in actions[:30]:
            print(f"  {x['ticker'][:36]:<38} {x['side']:<4} {x['qty']:>5} {x['sell_at']:>4}c "
                  f"${x['exposure']:>7.2f} ${x['expected_recovery']:>7.2f} {x['status']:>10}")
    if skipped:
        print()
        print(f"  SKIPPED ({len(skipped)} — no bid):")
        for x in skipped[:10]:
            print(f"  {x['ticker'][:36]:<38} pos={x['pos']:>+5.0f} exp=${x['exposure']:>6.2f} {x['reason']}")
    print()


if __name__ == "__main__":
    sys.exit(main() or 0)
