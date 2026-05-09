"""v3 paper executor — uses event-cap'd plan, places top-9 only."""
from __future__ import annotations
import json, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path

SOV = Path("/root/sovereign")
DB_PATH = SOV / "data" / "sovereign.db"
PLAN_PATH = SOV / "data" / "earnings_plan_may7_v3.json"
EVENT_ID = "earnings_may7_2026"


def main():
    plan = json.load(PLAN_PATH.open())
    trades = plan.get("actionable_trades", [])
    print(f"v3 plan: {len(trades)} actionable trades, ${plan['summary']['total_stake']:.2f} stake")

    conn = sqlite3.connect(str(DB_PATH), timeout=10)

    # Cancel ALL existing paper for this event (replace with v3)
    cancelled = conn.execute(
        "UPDATE sovereign_orders SET status='cancelled_v3_replace' "
        "WHERE event_id=? AND status='paper'", (EVENT_ID,)
    ).rowcount
    print(f"Cancelled {cancelled} v2 paper orders")
    conn.commit()

    placed = []; now_iso = datetime.now(timezone.utc).isoformat()
    for t in trades:
        ctrs = t.get("contracts_capped", t.get("contracts", 0))
        stake = t.get("stake_capped", t.get("stake", 0))
        if ctrs <= 0: continue
        order_id = f"PAPER-EARN-V3-{uuid.uuid4().hex[:10]}"
        cur = conn.execute(
            """INSERT INTO sovereign_orders(
                event_id, ticker, side, price_cents, contracts, cost_usd,
                order_id, status, reason, paper, placed_at, thesis_payload
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (EVENT_ID, t["ticker_kalshi"], t["side"].lower(),
             int(t["price"] * 100), ctrs, stake,
             order_id, "paper", "v3_recency_event_capped", 1, now_iso,
             json.dumps({k: t[k] for k in ("co","term","base_rate","k_w","n_w","conf","edge","price","rules") if k in t})),
        )
        placed.append((t, cur.lastrowid))
    conn.commit(); conn.close()

    print()
    print(f"✅ PLACED {len(placed)} v3 paper orders")
    print(f"  {'#':<3} {'CO':<5} {'TERM':<22} {'SIDE':<5} {'PRICE':>6} {'CTRS':>5} {'STAKE':>8}  ID")
    for i, (t, oid) in enumerate(placed, 1):
        print(f"  {i:<3} {t['co']:<5} {t['term'][:20]:<22} {t['side']:<5} {t['price']:>5.2f}c {t.get('contracts_capped', t.get('contracts',0)):>5} ${t.get('stake_capped', t.get('stake',0)):>6.2f}  sov_id={oid}")
    print()
    print(f"  Total: ${sum(t.get('stake_capped', t.get('stake', 0)) for t,_ in placed):.2f}")
    print(f"  Expected profit: ${plan['summary']['expected_profit']:.2f}")
    print(f"  ROI: {plan['summary']['roi_pct']:.1f}%")


if __name__ == "__main__":
    main()
