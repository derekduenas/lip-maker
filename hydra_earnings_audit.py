"""HYDRA-EARNINGS — autonomous per-trade pipeline auditor (Sonnet).

For each trade in the v3 plan, runs an 8-check audit:
  1. Liquidity      — vol/spread sanity vs filters
  2. Settle window  — event date matches
  3. Corpus signal  — k/n meaningful or Laplace-only?
  4. Term scope     — regex matches Kalshi rule scope
  5. Edge math      — verify (prob - price - fee)
  6. Kelly math     — verify (b·p - q)/b
  7. Recency        — recent quarters aligned with base?
  8. Risk fit       — size appropriate vs uncertainty?

Output: per-trade size_multiplier (0.0-1.0) + concerns + systemic flags.

Uses Sonnet (claude-sonnet-4-6) for math + reasoning.
Cost ~$0.05 per event (one batch call).
"""
from __future__ import annotations
import os, sys, json
from datetime import datetime, timezone
from pathlib import Path

SOV = Path("/root/sovereign")
PLAN_PATH = SOV / "data" / "earnings_plan_may7_v3.json"
AUDIT_OUT = SOV / "data" / "hydra_earnings_audit_may7.json"
MODEL = "claude-sonnet-4-6"


SYSTEM_PROMPT = """You are HYDRA-EARNINGS — earnings mention market trade auditor.

You audit EACH trade through 8 specific checks BEFORE money goes live.
You are NOT a primary signal. The corpus IS the truth. You verify mechanics + flag risks.

For each trade, run these 8 checks:

  1. LIQUIDITY     — vol > 50, spread < 20¢ (verify pipeline numbers reasonable)
  2. SETTLE WINDOW — event date in ticker = within 48h
  3. CORPUS SIGNAL — k_weighted / n_weighted: is base rate from REAL evidence or mostly Laplace prior?
                     k_w >= 0.5 AND n_w >= 1.5 → real signal. Else weak.
  IMPORTANT — MARKET MISPRICING INVERSION CASE:
  When base_rate > 0.5 and side='NO', this is NOT a bug. It means market YES is
  OVERPRICED. Example: corpus says 72% YES likely, but market YES @ 97¢ implies
  market thinks 97% YES. We correctly buy NO @ 4¢ because market YES is overpriced.
  Edge = (1 - base_rate) - no_price - fee. THIS IS CORRECT — do not flag inversion.

  4. TERM SCOPE    — Read the rules_primary text. Does our stem-matching regex
                     match the settlement scope? Concerns:
                     • "exactly 'X'" rules vs our stem matching
                     • Multi-word terms with spelling variants
                     • Q+A inclusion/exclusion
                     • Speaker scope (any rep vs CEO only)
  5. EDGE MATH     — Recompute: edge = our_prob - price - fee
                     fee = max(0.0044, 0.0175 × p × (1-p))
                     If your computed edge differs from claimed edge by >2pp, FAIL.
  6. KELLY MATH    — Recompute: b = (1-price)/price, p=our_prob, q=1-p
                     f* = (b·p - q) / b
                     Sanity: f_practical = min(f* × 0.25, 0.05)
                     stake = bankroll × f_practical
                     If contracts wildly different, FAIL.
  7. RECENCY       — Given k_w/n_w, is the recent-call pattern stable?
                     A term that appeared 4/4 calls is reliable.
                     A term that flipped 0/4 → 4/4 mid-period suggests trend, not stable.
  8. RISK FIT      — Given corpus uncertainty (low n_w), is stake size proportional?
                     If signal is weak, prefer smaller size.

PER-TRADE OUTPUT:
  confidence: HIGH | MED | LOW | SKIP
  size_multiplier: 1.0 (HIGH) | 0.75 (MED) | 0.5 (LOW) | 0.0 (SKIP)
  audit_passes: {liquidity, settle_window, corpus_signal, term_scope, edge_math, kelly_math, recency, risk_fit} = bool each
  concerns: array of 1-3 specific flags, like
            ["k=0/n=4 → mostly Laplace prior", "rule says exact term, regex stem-matches"]

SYSTEMIC CONCERNS:
  If 3+ trades share same concern (e.g., all "term scope ambiguous") → flag as systemic.
  If math errors across multiple trades → pipeline bug.

OUTPUT JSON ONLY (no preamble, no fences):
{
  "trade_audits": {
    "<ticker_kalshi>": {
      "confidence": "HIGH",
      "size_multiplier": 1.0,
      "audit_passes": {"liquidity": true, "settle_window": true, "corpus_signal": true, "term_scope": true, "edge_math": true, "kelly_math": true, "recency": true, "risk_fit": true},
      "concerns": []
    },
    ...
  },
  "systemic_concerns": ["..."],
  "summary": "X HIGH, Y MED, Z LOW, N SKIP — net adjustment +/- $X"
}"""


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        for line in open("/root/sovereign/.env").read().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip("'\"")
                break
    assert api_key, "no API key"

    plan = json.load(PLAN_PATH.open())
    trades = plan.get("actionable_trades", [])
    print(f"Loaded {len(trades)} trades from v3 plan")

    # Build context per trade
    audit_input = []
    for t in trades:
        audit_input.append({
            "ticker_kalshi": t["ticker_kalshi"],
            "co": t["co"], "term": t["term"],
            "rules": t.get("rules", ""),
            "side": t["side"], "price": t["price"],
            "base_rate": t["base_rate"],
            "k_weighted": t.get("k_w", 0), "n_weighted": t.get("n_w", 0),
            "n_raw_transcripts": t.get("n_raw", 0),
            "edge_claimed_pp": round(t["edge"] * 100, 2),
            "contracts": t.get("contracts_capped", t.get("contracts", 0)),
            "stake_usd": t.get("stake_capped", t.get("stake", 0)),
            "vol_24h": t.get("vol_24h", 0),
        })

    user_msg = (
        "Pipeline parameters:\n"
        f"  bankroll = $1000\n"
        f"  kelly_frac = 0.25 (quarter-Kelly)\n"
        f"  max_pct_per_trade = 5%\n"
        f"  max_pct_per_event = 40%\n"
        f"  fee_formula = max($0.0044, 1.75% × p × (1-p))\n\n"
        "Trades to audit:\n"
        + json.dumps(audit_input, indent=2)
    )

    print(f"\nCalling {MODEL}...")
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    r = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    import re as _re
    text = "".join(b.text for b in r.content if hasattr(b, "text"))
    m = _re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"no JSON in resp: {text[:300]}")
    text = m.group(0)
    cost = r.usage.input_tokens * 3e-6 + r.usage.output_tokens * 1.5e-5
    print(f"  Tokens in/out: {r.usage.input_tokens}/{r.usage.output_tokens}, cost: ${cost:.4f}\n")

    audit = json.loads(text)
    audit["_metadata"] = {"model": MODEL, "cost_usd": round(cost, 4),
                          "audited_at": datetime.now(timezone.utc).isoformat()}
    with open(AUDIT_OUT, "w") as f:
        json.dump(audit, f, indent=2)

    # Pretty print per-trade
    print("=" * 110)
    print("HYDRA-EARNINGS AUDIT RESULTS")
    print("=" * 110)
    audits = audit.get("trade_audits", {})
    print(f"  {'#':<3} {'CO':<5} {'TERM':<22} {'ORIG_$':>8} {'CONF':<5} {'MULT':>5} {'NEW_$':>8}  CONCERNS")
    total_orig = 0; total_new = 0
    for i, t in enumerate(trades, 1):
        tk = t["ticker_kalshi"]
        a = audits.get(tk, {})
        conf = a.get("confidence", "?")
        mult = float(a.get("size_multiplier", 1.0))
        orig_stake = t.get("stake_capped", t.get("stake", 0))
        new_stake = round(orig_stake * mult, 2)
        total_orig += orig_stake; total_new += new_stake
        concerns = "; ".join(a.get("concerns", []))[:50]
        flag = "🔥" if conf == "HIGH" else ("✓ " if conf == "MED" else (" ?" if conf == "LOW" else "🚫"))
        print(f"  {i:<3} {flag}{t['co']:<3} {t['term'][:20]:<22} ${orig_stake:>6.2f} {conf:<5} {mult:>4.2f}x ${new_stake:>6.2f}  {concerns}")

    print()
    print(f"  ORIGINAL: ${total_orig:.2f}  →  ADJUSTED: ${total_new:.2f}  (Δ ${total_new-total_orig:+.2f})")

    if audit.get("systemic_concerns"):
        print(f"\n  ⚠ SYSTEMIC CONCERNS:")
        for c in audit["systemic_concerns"]:
            print(f"    - {c}")
    print(f"\n  {audit.get('summary', '')}")
    print(f"\n  ✅ Audit saved: {AUDIT_OUT}")


if __name__ == "__main__":
    main()
