"""AEGIS STRATEGIC INTERVIEW — what would Sonnet do to scale LIP making to 6 figures?

Same "life depends on it" framing as Sovereign but applied to AEGIS.
"""
import os, json
from datetime import datetime, timezone
from pathlib import Path

SOV = Path("/root/sovereign")
LIP = Path("/root/lip-maker")

PROMPT = """You are a top-tier market-making PM. Citadel/Jane Street experience. You understand
Kalshi LIP scoring (size × DF^(best - price) × time), maker-taker spreads, queue position,
adverse selection, and inventory risk. You've seen retail market makers get crushed and
institutional ones print money.

Your job: review the AEGIS LIP-maker system below, then RANK the top 10 changes/builds
that would scale this to $100k+/year. Life-or-death prioritization. NO fluff.

CURRENT AEGIS STATE (real money, running on Kalshi as of 2026-05-07):
  • Live NAV: $2,297 (cash $494, positions MTM $1,802)
  • Started Apr 19 with $80, currently 6-day NAV +$67 trend
  • Lifetime Kalshi LIP rewards PAID: $319.22 (May $90.58 so far)
  • Live mode: yes (real money, not paper)

FUNNEL STATE:
  • 3,605 active LIP programs Kalshi-wide
  • 3,186 we are ENROLLED in (after MIN_DISCOUNT_FACTOR 0.5→0.2)
  • Top-30 by reward_per_day actively prioritized
  • ~10 resting orders at any moment (heavy churn)
  • 50-70 open positions

7-DAY P&L:
  • Realized inventory PnL: -$425
  • LIP rebate earned: +$70
  • NET: +$45 (barely positive)
  • Daily NAV grinding +$10-30/day

INFRASTRUCTURE BUILT:
  • Quote loop: joins best YES + NO bid on every WS book update
  • Bleed monitor (every 10min): blacklists series with >30% MTM loss + LIP-active whitelist
  • Zombie liquidator (every 30min): closes positions in expired LIP programs
  • Stranded liquidator (every 5min): exits dropped-coverage positions
  • Capital allocator (every 30min): picks top yield-per-$ from 3,186 enrolled
  • Toxicity filter: blacklists 100% one-sided fill markets
  • Series auto-prune (daily): drops -$5+ losers over 3 settles
  • Periodic discover: auto-finds new LIP programs every 30min
  • Pre-trade EV check: refuses where adverse_cost > rebate
  • Rules-based ramp: bankroll cap at 40% (was at 10% before bug fix)

PROBLEMS / BOTTLENECKS:
  • Realized losses ~ 6× rebate income (we're MM'ing toxic flow)
  • Capital constrained: bankroll cap = $800 at $2k NAV, often hit
  • Adverse selection: get filled when wrong, not filled when right
  • LIP rewards are SMALL per market: $20-80/day per top market
  • Volume Incentive (VIP) accruing only $1.30/week
  • At $2k bankroll, math says $300-800/mo realistic — not $20k

GOALS:
  • Scale to $100k+/year (was Citadel-quoter-style profitable)
  • Real money, not paper
  • Solo operator with Claude Code

CONSTRAINTS:
  • $0-50/mo ongoing OK
  • Real Kalshi account, real money
  • Cannot get banned (no spoofing, post-only orders, follow Kalshi rules)

RANK TOP 10 BUILDS / CHANGES.

For each, give:
  - WHY this matters (1 sentence + quantification)
  - WHAT specifically to do (concrete, not "use AI better")
  - HOW MUCH effort (hours)
  - HOW MUCH expected lift ($/year)
  - WHY this BEATS what they're doing now

Be ruthlessly specific. Skip ideas that won't move the needle.

Format: JSON list of 10 ranked items, each with {rank, title, why, what, effort_hrs, expected_lift_usd, beats_what}."""


def main():
    api_key = ""
    for line in open(LIP / ".env").read().splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            api_key = line.split("=", 1)[1].strip().strip("'\"")
            break
    assert api_key

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    print("Asking Sonnet for the AEGIS 6-figure architecture...\n")

    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        messages=[{"role": "user", "content": PROMPT}],
    )
    text = "".join(b.text for b in r.content if hasattr(b, "text"))
    cost = r.usage.input_tokens * 3e-6 + r.usage.output_tokens * 1.5e-5
    print(f"Tokens in/out: {r.usage.input_tokens}/{r.usage.output_tokens}, cost: ${cost:.4f}\n")

    out = SOV / "data" / "aegis_strategic_interview_may7.json"
    with open(out, "w") as f:
        json.dump({"prompt": PROMPT, "response": text,
                   "cost_usd": round(cost, 4),
                   "ts": datetime.now(timezone.utc).isoformat()}, f, indent=2)

    print("=" * 110)
    print("SONNET'S AEGIS 6-FIGURE ARCHITECTURE")
    print("=" * 110)
    print(text)
    print()
    print(f"  Saved: {out}")


if __name__ == "__main__":
    main()
