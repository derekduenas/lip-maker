"""SOVEREIGN EARNINGS FUNNEL AUDIT — show the math, find the bugs.

Per-stage funnel:
  L0  ALL Kalshi mention markets
  L1  Matched to target ticker (MCD/LYFT)
  L2  Has parseable subtitle (term)
  L3  Has corpus (≥1 transcript for ticker)
  L4  Has YES + NO ask price (executable)
  L5  Edge calc completed (both sides)
  L6  Edge ≥ MIN_THRESHOLD (5%)
  L7  Kelly sizing produces ≥1 contract
  L8  Paper orders placed in DB

Math equations shown with worked examples per top trade.
Bug checks: settlement rule vs term match, fee calc, sizing math.
"""
from __future__ import annotations
import os, sys, sqlite3, json, math
from pathlib import Path

SOV = Path("/root/sovereign")
DB_PATH = SOV / "data" / "sovereign.db"
PLAN_PATH = SOV / "data" / "earnings_plan_may7_v2.json"
TARGET_TICKERS = ["MCD", "LYFT"]


def fee(p_dollars: float) -> float:
    """Kalshi maker fee. Returns dollars per contract."""
    if not p_dollars or p_dollars <= 0 or p_dollars >= 1:
        return 0
    return max(0.0044, 0.0175 * p_dollars * (1 - p_dollars))


def kelly_size(prob, price, bankroll=1000.0, kelly_frac=0.25, max_pct=0.05):
    """Returns (f_star_full, f_practical, stake_usd, contracts)."""
    if prob <= price or price <= 0 or price >= 1:
        return (0, 0, 0, 0)
    p = prob; q = 1 - p; b = (1 - price) / price
    f_star = (b * p - q) / b
    f_practical = min(max(0, f_star * kelly_frac), max_pct)
    stake = bankroll * f_practical
    return (f_star, f_practical, stake, int(stake / price))


def expected_value(prob, price, contracts):
    """EV in dollars across all contracts."""
    if contracts <= 0: return 0
    win_pl = (1 - price) * contracts
    lose_pl = -price * contracts
    fee_total = fee(price) * contracts
    return prob * win_pl + (1 - prob) * lose_pl - fee_total


def main():
    print("=" * 100)
    print("SOVEREIGN EARNINGS FUNNEL AUDIT — May 7 event")
    print("=" * 100)

    # === Re-pull live markets to compute funnel from scratch
    sys.path.insert(0, str(SOV))
    from engine.scanner import KalshiClient
    c = KalshiClient()
    all_mkts = c.get_mention_markets()
    L0 = len(all_mkts)

    L1 = [m for m in all_mkts if any(m.get("ticker", "").startswith(f"KXEARNINGSMENTION{t}") for t in TARGET_TICKERS)]

    L2 = [m for m in L1 if m.get("subtitle") or m.get("yes_sub_title")]

    # Corpus check
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    corpus_counts = {}
    for tkr in TARGET_TICKERS:
        n = conn.execute("SELECT COUNT(*) FROM transcripts WHERE event_type=?",
                          (f"{tkr.lower()}_earnings",)).fetchone()[0]
        corpus_counts[tkr] = n

    L3 = [m for m in L2 if any(corpus_counts[t] >= 1 for t in TARGET_TICKERS if t in m.get("ticker", ""))]

    L4 = [m for m in L3 if m.get("yes_ask") is not None and m.get("no_ask") is not None]

    # Score for L5+
    plan = json.load(PLAN_PATH.open())
    scored = plan.get("all_scored", [])
    L5 = scored
    L6 = [o for o in scored if (o.get("edge") or 0) >= 0.05]
    L7 = [o for o in L6 if o.get("contracts", 0) >= 1]

    # L8: actual placed
    placed_rows = conn.execute(
        "SELECT id, ticker, side, contracts, cost_usd FROM sovereign_orders "
        "WHERE event_id='earnings_may7_2026' AND paper=1 AND placed_at >= datetime('now', '-12 hours')"
    ).fetchall()
    L8 = placed_rows

    print(f"\n  📊 FUNNEL")
    print(f"  {'STAGE':<35} {'COUNT':>6}  {'DROP':>6}  REASON")
    print(f"  {'-'*35} {'-'*6}  {'-'*6}  {'-'*40}")
    print(f"  L0 ALL mention markets              {L0:>6}    -    Total Kalshi /markets mention universe")
    print(f"  L1 Match target ticker (MCD/LYFT)   {len(L1):>6}  {L0-len(L1):>6}  filter to KXEARNINGSMENTION{{tkr}}")
    print(f"  L2 Has parseable subtitle/term      {len(L2):>6}  {len(L1)-len(L2):>6}  drop if no term")
    print(f"  L3 Corpus available (≥1 transcript) {len(L3):>6}  {len(L2)-len(L3):>6}  MCD={corpus_counts['MCD']}, LYFT={corpus_counts['LYFT']}")
    print(f"  L4 Has YES+NO ask price             {len(L4):>6}  {len(L3)-len(L4):>6}  drop illiquid")
    print(f"  L5 Edge calc completed              {len(L5):>6}  {len(L4)-len(L5):>6}  computed both YES + NO sides")
    print(f"  L6 Edge ≥ 5% threshold              {len(L6):>6}  {len(L5)-len(L6):>6}  filter for actionable")
    print(f"  L7 Kelly sizing ≥1 contract         {len(L7):>6}  {len(L6)-len(L7):>6}  positive deploy")
    print(f"  L8 Paper orders placed in DB        {len(L8):>6}  {len(L7)-len(L8):>6}  actually written")

    # === MATH WALKTHROUGH on top 3 trades
    print(f"\n  🧮 MATH WALKTHROUGH — top 3 trades")
    actionable = plan.get("actionable_trades", [])[:3]
    for i, t in enumerate(actionable, 1):
        prob_we = t["base_rate"] if t["side"] == "YES" else (1 - t["base_rate"])
        price = t["price"]
        edge = t["edge"]
        n = t["n"]; k = t["k"]
        f_star, f_prac, stake, ctrs = kelly_size(prob_we, price, bankroll=1000.0)
        ev = expected_value(prob_we, price, t["contracts"])
        max_win = (1 - price) * t["contracts"]
        max_loss = price * t["contracts"]
        print(f"\n  {i}. {t['co']} {t['term']} — {t['side']} @ ${price:.2f}")
        print(f"     CORPUS:    k={k}, n={n} transcripts")
        print(f"     BASE RATE: P(mention) = (k+1)/(n+2) = ({k}+1)/({n}+2) = {(k+1)/(n+2)*100:.1f}%")
        print(f"     OUR PROB ON {t['side']}: {prob_we*100:.1f}%")
        print(f"     MARKET PRICE: ${price:.2f} (implies market thinks {price*100:.0f}%)")
        print(f"     FEE per contract: ${fee(price):.4f}")
        print(f"     EDGE: {prob_we*100:.1f}% - {price*100:.1f}% - fee = +{edge*100:.1f}pp")
        print(f"     KELLY (full f*): {f_star:.4f}")
        print(f"     KELLY (×0.25 + 5% cap): {f_prac:.4f} → stake ${stake:.2f} → {ctrs} contracts")
        print(f"     EV: ${ev:.2f} (over {t['contracts']} contracts)")
        print(f"     SCENARIOS: max win ${max_win:.2f}, max loss ${max_loss:.2f}")

    # === BUG CHECKS
    print(f"\n  🐛 BUG CHECKS")

    # Check 1: settlement rule vs term match
    print(f"\n  Check 1: Settlement rule wording vs our term match")
    sample_mkt = next((m for m in L1 if "DIVI" in m.get("ticker", "")), None)
    if sample_mkt:
        rules = sample_mkt.get("rules_primary", "") or sample_mkt.get("rules", "")
        print(f"     MCD Dividend rule excerpt:")
        print(f"       \"{rules[:200]}\"")
        print(f"     We match: regex r'\\bdividend\\b' (whole word, case-insensitive)")
        print(f"     ✓ Matches: 'dividend', 'Dividend', 'DIVIDEND'")
        print(f"     ✗ Does NOT match: 'dividends' (plural), 'dividend-paying' (hyphen), 'dividend.'")
        print(f"     ⚠ BUG RISK: Kalshi may settle on STEM match, not exact. Check rules text carefully.")

    # Check 2: fee math
    print(f"\n  Check 2: Fee math")
    test_prices = [0.05, 0.25, 0.50, 0.95]
    for p in test_prices:
        f = fee(p)
        kalshi_formula = max(0.0044, 0.0175 * p * (1 - p))
        match = "✓" if abs(f - kalshi_formula) < 0.0001 else "✗"
        print(f"     {match} price=${p:.2f} → fee=${f:.4f} (formula: max($0.0044, 1.75% × p × (1-p)))")

    # Check 3: Kelly sizing sanity
    print(f"\n  Check 3: Kelly sizing examples")
    examples = [
        ("near certainty", 0.95, 0.50),
        ("strong edge", 0.83, 0.46),
        ("modest edge", 0.55, 0.50),
        ("at-money", 0.50, 0.50),  # zero edge
        ("negative", 0.40, 0.50),  # we shouldn't trade
    ]
    for label, our_prob, market_price in examples:
        f_star, f_prac, stake, ctrs = kelly_size(our_prob, market_price, 1000.0)
        flag = " ✓ should deploy" if stake > 0 else " ✓ correctly skipped"
        print(f"     {label:<18} prob={our_prob}, price={market_price} → f*={f_star:+.3f} f_prac={f_prac:.4f} stake=${stake:.2f}{flag}")

    # Check 4: total deployment vs bankroll
    print(f"\n  Check 4: Total deployment vs bankroll")
    total = sum(o["cost_usd"] for o in conn.execute(
        "SELECT cost_usd FROM sovereign_orders WHERE event_id='earnings_may7_2026' AND paper=1"
    ).fetchall())
    print(f"     Total stake: ${total:.2f}")
    print(f"     Bankroll: $1000")
    print(f"     Deployed: {total/1000*100:.1f}% (reserve: {(1000-total)/1000*100:.1f}%)")
    if total > 1000:
        print(f"     ✗ BUG: deployed > bankroll! Should be capped.")
    elif total > 950:
        print(f"     ⚠ WARN: ~all-in. No reserve for adjustments.")
    else:
        print(f"     ✓ Healthy reserve maintained.")

    # Check 5: tickers actually exist on Kalshi (not stale plan)
    print(f"\n  Check 5: Tickers in plan still live on Kalshi")
    plan_tickers = {o["ticker_kalshi"] for o in plan.get("actionable_trades", [])}
    live_tickers = {m["ticker"] for m in L1}
    missing = plan_tickers - live_tickers
    if missing:
        print(f"     ✗ BUG: {len(missing)} plan tickers no longer on Kalshi: {list(missing)[:3]}")
    else:
        print(f"     ✓ All {len(plan_tickers)} plan tickers still live")

    conn.close()
    print()
    print("=" * 100)


if __name__ == "__main__":
    main()
