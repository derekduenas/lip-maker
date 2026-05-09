"""SOVEREIGN STRATEGIC INTERVIEW — ask Sonnet what it would do if life depended on it.

One Claude call, no truncation. Sonnet acts as a top-tier quantitative PM with
prediction-market alpha experience. We give it the full state of our system.
It returns prioritized recommendations to scale to 6 figures.
"""
import os, json, sys
from datetime import datetime, timezone
from pathlib import Path

SOV = Path("/root/sovereign")

INTERVIEW_PROMPT = """You are a top-tier quant fund PM with deep experience in mention markets,
prediction markets, and arbitrage. You've seen the trader who turned $313 into $400K on
Polymarket via latency arb (now killed by fees). You know:
  - Renaissance Medallion's actual edge sources
  - Polymarket/Kalshi market microstructure
  - NLP-driven event prediction
  - Earnings call prediction
  - Why most retail traders fail in prediction markets

The user has built a Sovereign Earnings system. Your job: review what they have, then
RANK the top 10 changes/builds that would scale this to $100k+/year (6 figures) in net
profit. Life-or-death prioritization. NO fluff, NO platitudes.

CURRENT SYSTEM (what they have):
  • Sovereign EARNINGS pipeline (v4 god-mode, just shipped tonight 2026-05-07):
    - Motley Fool transcript scraper for any ticker
    - Stem-matching mention detection (catches plurals/hyphens)
    - Recency-weighted Laplace base rates (half-life 6 months)
    - Edge calc: prob - market_price - Kalshi maker fee
    - Quarter-Kelly sizing with 5% per-trade + 40% per-event caps
    - HYDRA-EARNINGS Sonnet auditor (8-check per-trade audit, autonomous size adjust)
  • Current corpus: MCD 4 transcripts, LYFT 4 transcripts (only 2 tickers)
  • First test event: MCD + LYFT earnings May 7 2026 (tomorrow morning)
  • Paper bankroll: $1,000. Per-event cap $400. 9 trades placed, $275 stake (after audit)
  • Expected profit per event (this size): $80-120
  • Real Kalshi account NAV: ~$2,100 (separate AEGIS LIP rebate engine running)

COMPETITION:
  • Most Kalshi market makers don't read 8 quarters of transcripts
  • SIG / Citadel cover ~10 high-volume names, ignore long-tail
  • Polymarket's arbitrage was killed by dynamic taker fees in 2025

CONSTRAINTS:
  • Solo operator (1 person + Claude)
  • $0 ongoing cost ideal, <$50/mo acceptable
  • Real money risk gating: validate paper before scaling
  • Goal: replicable $100k+/year from THIS strategy alone

RANK THE TOP 10 BUILDS / CHANGES.

For each, give:
  - WHY this matters (1 sentence, with quantification)
  - WHAT specifically to do (concrete, not "use AI better")
  - HOW MUCH effort (hours)
  - HOW MUCH expected profit lift ($/year)
  - WHY this BEATS what they're doing now

Be ruthlessly prioritized. Skip ideas that won't move the needle.

Format: JSON list of 10 ranked items, each with {rank, title, why, what, effort_hrs, expected_lift_usd, beats_what}."""


def main():
    api_key = ""
    for line in open(SOV / ".env").read().splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            api_key = line.split("=", 1)[1].strip().strip("'\"")
            break
    assert api_key

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    print("Asking Sonnet for the 6-figure architecture...\n")

    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        messages=[{"role": "user", "content": INTERVIEW_PROMPT}],
    )
    text = "".join(b.text for b in r.content if hasattr(b, "text"))
    cost = r.usage.input_tokens * 3e-6 + r.usage.output_tokens * 1.5e-5
    print(f"Tokens in/out: {r.usage.input_tokens}/{r.usage.output_tokens}, cost: ${cost:.4f}\n")

    # Save raw
    out = SOV / "data" / "strategic_interview_may7.json"
    with open(out, "w") as f:
        json.dump({"prompt": INTERVIEW_PROMPT, "response": text,
                   "cost_usd": round(cost, 4),
                   "ts": datetime.now(timezone.utc).isoformat()}, f, indent=2)

    # Print response
    print("=" * 110)
    print("SONNET'S 6-FIGURE ARCHITECTURE (LIFE-DEPENDS-ON-IT MODE)")
    print("=" * 110)
    print(text)
    print()
    print(f"  Saved: {out}")


if __name__ == "__main__":
    main()
