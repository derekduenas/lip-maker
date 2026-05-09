"""SOVEREIGN PAPER EXECUTOR — places paper orders for tonight's earnings event.

Reads earnings_plan_may7_v2.json. For each actionable trade:
  1. Records as paper order in sovereign_orders table (status='paper')
  2. Stores full thesis (base rate, edge, sizing) in thesis_payload JSON
  3. After event settles tomorrow, settle script will compute PnL

Honest paper semantics:
  - We commit to BUY at current ask price (no slippage simulated)
  - When market resolves, win = $1.00/contract if our side, $0 if other
  - PnL = (resolution_value - entry_price) × contracts
"""
from __future__ import annotations
import os, sys, json, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path

SOV = Path("/root/sovereign")
DB_PATH = SOV / "data" / "sovereign.db"
PLAN_PATH = SOV / "data" / "earnings_plan_may7_v2.json"
EVENT_ID = "earnings_may7_2026"


def main():
    if not PLAN_PATH.exists():
        print(f"❌ Plan not found: {PLAN_PATH}")
        return 1
    plan = json.load(PLAN_PATH.open())
    trades = plan.get("actionable_trades", [])
    print(f"Plan generated at: {plan['generated_at']}")
    print(f"Bankroll: ${plan['bankroll']}")
    print(f"Actionable trades: {len(trades)}")
    print(f"Total stake: ${plan['summary']['total_stake_usd']:.2f}")
    print(f"Expected profit: ${plan['summary']['expected_profit_usd']:.2f}")
    print()

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    placed = []
    skipped = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for t in trades:
        if t["contracts"] <= 0:
            skipped.append((t, "zero contracts"))
            continue

        # Check if already placed (idempotent)
        existing = conn.execute(
            "SELECT id FROM sovereign_orders WHERE ticker=? AND event_id=? AND paper=1 AND status='paper' AND placed_at >= datetime('now', '-12 hours')",
            (t["ticker_kalshi"], EVENT_ID),
        ).fetchone()
        if existing:
            skipped.append((t, f"already placed (id={existing[0]})"))
            continue

        order_id = f"PAPER-EARN-{uuid.uuid4().hex[:12]}"
        price_cents = int(t["price"] * 100)

        cur = conn.execute(
            """INSERT INTO sovereign_orders(
                event_id, ticker, side, price_cents, contracts, cost_usd,
                order_id, status, reason, paper, placed_at, thesis_payload
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                EVENT_ID, t["ticker_kalshi"], t["side"].lower(),
                price_cents, t["contracts"], t["stake_usd"],
                order_id, "paper", "earnings_mention_corpus_score", 1, now_iso,
                json.dumps({
                    "co": t["co"], "term": t["term"],
                    "base_rate": t["base_rate"], "k": t["k"], "n": t["n"], "conf": t["conf"],
                    "edge": t["edge"], "kelly_frac": t["kelly_frac"],
                    "yes_ask": t["yes_ask"], "no_ask": t["no_ask"],
                }),
            ),
        )
        placed.append((t, cur.lastrowid))

    conn.commit()
    conn.close()

    print("=" * 100)
    print(f"PLACED {len(placed)} paper orders | SKIPPED {len(skipped)}")
    print("=" * 100)
    if placed:
        print(f"  {'#':<3} {'CO':<5} {'TERM':<22} {'SIDE':<5} {'PRICE':>6} {'CTRS':>5} {'STAKE':>8}  ID")
        for i, (t, oid) in enumerate(placed, 1):
            print(f"  {i:<3} {t['co']:<5} {t['term'][:20]:<22} {t['side']:<5} {t['price']:>5.2f}c {t['contracts']:>5} ${t['stake_usd']:>6.2f}  sov_id={oid}")
    if skipped:
        print(f"\n  SKIPPED ({len(skipped)}):")
        for t, why in skipped[:5]:
            print(f"    {t['co']:<5} {t['term'][:25]:<27} — {why}")

    total = sum(t['stake_usd'] for t, _ in placed)
    exp_profit = sum(t['edge'] * t['contracts'] * t['price'] for t, _ in placed)
    print()
    print(f"  TOTAL DEPLOYED: ${total:.2f}")
    print(f"  EXPECTED PROFIT (if base rates accurate): ${exp_profit:.2f}")
    print(f"  ROI on deployed: {exp_profit/max(0.01,total)*100:.1f}%")
    print()
    print(f"  ✅ Paper orders saved. After MCD/LYFT calls tomorrow, run settlement script to score actuals.")


if __name__ == "__main__":
    main()
