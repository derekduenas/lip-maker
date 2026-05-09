"""v4 paper executor — applies HYDRA-EARNINGS audit adjustments autonomously.

Reads:
  - earnings_plan_may7_v3.json (raw pipeline plan)
  - hydra_earnings_audit_may7.json (Sonnet per-trade adjustments)

For each trade:
  - new_contracts = original_contracts × size_multiplier
  - new_stake    = original_stake × size_multiplier
  - if SKIP, skip entirely
  - log original + adjustment + concerns to thesis_payload

Cancels v3 paper orders (event_id=earnings_may7_2026, status=paper),
then places v4 with adjusted sizes.
"""
from __future__ import annotations
import json, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path

SOV = Path("/root/sovereign")
DB_PATH = SOV / "data" / "sovereign.db"
PLAN_PATH = SOV / "data" / "earnings_plan_may7_v3.json"
AUDIT_PATH = SOV / "data" / "hydra_earnings_audit_may7.json"
EVENT_ID = "earnings_may7_2026"


def main():
    plan = json.load(PLAN_PATH.open())
    audit = json.load(AUDIT_PATH.open())
    audits = audit.get("trade_audits", {})

    trades = plan.get("actionable_trades", [])
    print(f"v3 plan: {len(trades)} trades, ${plan['summary']['total_stake']:.2f}")
    print(f"v4 audit: {audit['_metadata']['model']} (cost ${audit['_metadata']['cost_usd']:.4f})")

    conn = sqlite3.connect(str(DB_PATH), timeout=10)

    # Cancel current v3 paper orders
    cancelled = conn.execute(
        "UPDATE sovereign_orders SET status='cancelled_v4_audit_replace' "
        "WHERE event_id=? AND status='paper'", (EVENT_ID,)
    ).rowcount
    conn.commit()
    print(f"Cancelled {cancelled} v3 orders\n")

    placed = []; skipped = []; now_iso = datetime.now(timezone.utc).isoformat()

    for t in trades:
        tk = t["ticker_kalshi"]
        a = audits.get(tk, {})
        mult = float(a.get("size_multiplier", 1.0))
        conf = a.get("confidence", "?")
        concerns = a.get("concerns", [])
        passes = a.get("audit_passes", {})

        orig_ctrs = t.get("contracts_capped", t.get("contracts", 0))
        orig_stake = t.get("stake_capped", t.get("stake", 0))
        new_ctrs = int(orig_ctrs * mult)
        new_stake = round(orig_stake * mult, 2)

        if mult == 0 or new_ctrs == 0:
            skipped.append((t, conf, concerns))
            continue

        order_id = f"PAPER-EARN-V4-{uuid.uuid4().hex[:10]}"
        cur = conn.execute(
            """INSERT INTO sovereign_orders(
                event_id, ticker, side, price_cents, contracts, cost_usd,
                order_id, status, reason, paper, placed_at, thesis_payload
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (EVENT_ID, tk, t["side"].lower(),
             int(t["price"] * 100), new_ctrs, new_stake,
             order_id, "paper", f"v4_hydra_audited_{conf}", 1, now_iso,
             json.dumps({
                 "co": t["co"], "term": t["term"],
                 "base_rate": t["base_rate"], "k_w": t.get("k_w"),
                 "n_w": t.get("n_w"), "edge": t["edge"],
                 "original_contracts": orig_ctrs, "original_stake": orig_stake,
                 "audit_confidence": conf, "audit_multiplier": mult,
                 "audit_concerns": concerns, "audit_passes": passes,
             })),
        )
        placed.append((t, cur.lastrowid, conf, mult, new_stake, new_ctrs, concerns))
    conn.commit(); conn.close()

    # PRINT result
    print("=" * 110)
    print(f"✅ V4 EXECUTOR — PLACED {len(placed)}, SKIPPED {len(skipped)} (HYDRA-EARNINGS audited)")
    print("=" * 110)
    print(f"  {'#':<3} {'CO':<5} {'TERM':<22} {'CONF':<5} {'MULT':>4} {'CTRS':>5} {'STAKE':>8}  CONCERN")
    for i, (t, oid, conf, mult, stake, ctrs, concerns) in enumerate(placed, 1):
        flag = "🔥" if conf == "HIGH" else ("✓ " if conf == "MED" else " ?")
        c = concerns[0][:50] if concerns else ""
        print(f"  {i:<3} {flag}{t['co']:<3} {t['term'][:20]:<22} {conf:<5} {mult:>3.2f}x {ctrs:>5} ${stake:>6.2f}  {c}")

    if skipped:
        print(f"\n  🚫 SKIPPED ({len(skipped)}):")
        for t, conf, concerns in skipped:
            c = concerns[0][:60] if concerns else ""
            print(f"    {t['co']:<5} {t['term'][:22]:<24} {conf:<5}  {c}")

    total = sum(s for _, _, _, _, s, _, _ in placed)
    print()
    print(f"  TOTAL DEPLOYED: ${total:.2f} (was ${plan['summary']['total_stake']:.2f}, Δ ${total-plan['summary']['total_stake']:+.2f})")

    if audit.get("systemic_concerns"):
        print(f"\n  ⚠ HYDRA-EARNINGS systemic concerns:")
        for c in audit["systemic_concerns"][:3]:
            print(f"    - {c[:120]}")


if __name__ == "__main__":
    main()
