"""HYDRA-SKEW PREDICTOR — proactive directional bias for AEGIS quotes.

THE PROBLEM:
  AEGIS posts equal-size YES + NO quotes on every LIP market (required for
  rebate score). Informed flow hits the side that's "right", accumulating
  bad inventory. Existing inventory skew (#97) reacts AFTER bleed.

THIS TOOL:
  Predicts BEFORE quoting which side is toxic. Per-market HYDRA Haiku call
  reads market context (price, title, time-to-close, recent fill bias) and
  returns skew_yes ∈ [0.3, 0.7]:
    - skew_yes = 0.5  → neutral (post equal both sides)
    - skew_yes = 0.7  → lean YES heavy, NO light
                        (predict YES side is SAFE; informed sells YES at
                         our high YES quotes, leaving us long YES — but
                         we PREDICT YES is correct so it's fine)
    - skew_yes = 0.3  → lean NO heavy, YES light

  Writes predictions to market_skew_hint table. Quote manager reads this
  and applies it as size_override before posting.

HOW THE LLM DECIDES (prompt logic):
  - Sharp consensus near 50%: high info value → likely toxic
  - Already at extreme (5% or 95%): efficient → no skew needed
  - Public-data driven (weather, macro): probably efficient
  - News/sentiment driven: more vulnerable to informed flow
  - Recent fill imbalance: if 80% of fills hit YES side, sharps likely
    accumulating NO inventory by selling YES — we should LEAN NO HEAVY

USAGE:
  python3 hydra_skew_predictor.py            # default (top 30 markets)
  python3 hydra_skew_predictor.py --json
  python3 hydra_skew_predictor.py --markets 50
  python3 hydra_skew_predictor.py --dry-run  # no DB writes

CRON: every 15min */15 * * * *
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

LIP_PATH = Path("/root/lip-maker")
LIP_DB = LIP_PATH / "data" / "lip_maker.db"
DEFAULT_N = 30
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SKEW_TTL_MINUTES = 30


SYSTEM_PROMPT = """You are HYDRA-SKEW — directional toxicity predictor for AEGIS market maker.

For each Kalshi market AEGIS quotes, predict which side will accumulate adverse
inventory (informed flow hits one side disproportionately). Return a "skew_yes"
score per market in [0.3, 0.7]:

  - skew_yes = 0.50  → NEUTRAL (post equal both sides; can't predict bias)
  - skew_yes > 0.55  → LEAN YES (mild) (post MORE YES size, LESS NO size)
                       Use when YES side appears SAFE (consensus matches our YES,
                       informed flow buys YES = we sell to them = no inventory).
                       Equivalently: NO side is TOXIC (informed flow sells NO at
                       our quotes, we accumulate long YES which is fine).
  - skew_yes < 0.45  → LEAN NO (mild) (post MORE NO size, LESS YES size)
                       Use when NO side appears safer.

KEY SIGNALS:
  • Market price (mid) far from 50 → already efficient, no skew (return 0.5)
  • Mid in 30-70% range with HIGH volume → high info content, predict bias from public data
  • Recent fill imbalance: if recent_yes_fill_pct > 0.7 → sharps buying YES heavily
    → they expect YES to win → we should SHORT YES (skew_yes = 0.35)
  • Title sentiment + rules: e.g. "Will [politician] resign by [date]"
    if no major news, the answer is usually NO → skew_yes = 0.40
  • Time to close: longer = more uncertainty = lean toward neutral
  • Public-data markets (weather/macro at known close): MAY be already arb'd
    → return 0.5 unless you see VERY recent fill pattern

CONSENSUS PRINCIPLE (CRITICAL):
  - Only deviate from 0.5 when MARKET MID AGREES with your direction.
  - Example: market mid 75 (consensus YES) + you also predict YES → skew_yes = 0.55
  - Example: market mid 75 + you predict NO → return 0.5 (don'''t fight the market)
  - Fighting the market is contrarian betting, NOT market making

CONSERVATIVE PRINCIPLES:
  - When in doubt → return 0.5 (neutral)
  - NEVER go outside [0.40, 0.60] — being WRONG hurts MORE than being RIGHT helps (we need both sides resting for LIP score)
  - Confidence 0.0-1.0 (only AEGIS applies skew when confidence > 0.5)

OUTPUT JSON ONLY (no preamble, no fences):
{
  "predictions": [
    {"ticker": "KXFOO-26-T1", "skew_yes": 0.45, "confidence": 0.6, "reason": "5-word reason"},
    ...
  ]
}"""


def _get_top_markets(n=DEFAULT_N):
    """Pull markets to predict skew on. Priority:
      1. Recent-fill markets (proven active books) — first 50%
      2. Top enrolled by reward_per_day (backfill) — remaining 50%
    Bug fix 2026-05-06: predictor was scoring theoretical top-LIP markets but
    AEGIS fills happen on a different set. Now sources from real fill activity."""
    conn = sqlite3.connect(str(LIP_DB), timeout=10)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    active = conn.execute(
        """SELECT f.ticker, p.reward_per_day_usd, p.target_size, p.end_date,
                  COUNT(*) AS fc
           FROM fill_ledger f
           JOIN lip_programs p ON p.market_ticker = f.ticker
           WHERE f.created_at >= ? AND p.end_date > date('now') AND p.paid_out=0
           GROUP BY f.ticker ORDER BY fc DESC LIMIT ?""",
        (cutoff, n // 2),
    ).fetchall()
    seen = {r[0] for r in active}
    backfill = conn.execute(
        """SELECT market_ticker, reward_per_day_usd, target_size, end_date, 0
           FROM lip_programs
           WHERE end_date > date('now') AND paid_out=0 AND enrolled=1
           ORDER BY reward_per_day_usd DESC NULLS LAST LIMIT ?""",
        (n,),
    ).fetchall()
    rows = list(active) + [r for r in backfill if r[0] not in seen][:n - len(active)]
    conn.close()
    return [{"ticker": r[0], "lip_per_day": r[1] or 0,
             "target_size": r[2] or 0, "close_date": r[3],
             "recent_fills": r[4] if len(r) > 4 else 0} for r in rows]


def _enrich_market(c, ticker):
    """Pull market mid + title + recent fill bias."""
    try:
        r = c.get("/markets", params={"tickers": ticker})
        m = (r.get("markets") or [None])[0]
        if not m:
            return None
        def to_cents(v):
            try: return round(float(v) * 100)
            except: return None
        ya = to_cents(m.get("yes_ask_dollars"))
        yb = to_cents(m.get("yes_bid_dollars"))
        mid = (yb + ya) / 2 if (ya and yb) else None
        return {
            "title": (m.get("title") or "")[:100],
            "rules": (m.get("rules_primary") or "")[:200],
            "yes_bid_c": yb, "yes_ask_c": ya, "mid_c": mid,
            "vol_24h":  float(m.get("volume_24h_fp") or 0),
            "close_time": m.get("close_time"),
            "status": m.get("status"),
        }
    except Exception:
        return None


def _recent_fill_bias(ticker, hours=2):
    """Return (yes_fills, no_fills, total) in last N hours."""
    conn = sqlite3.connect(str(LIP_DB), timeout=10)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    row = conn.execute(
        """SELECT
              SUM(CASE WHEN side='yes' THEN count ELSE 0 END),
              SUM(CASE WHEN side='no'  THEN count ELSE 0 END),
              SUM(count)
           FROM fill_ledger
           WHERE ticker=? AND created_at >= ?""",
        (ticker, cutoff),
    ).fetchone()
    conn.close()
    yes_n, no_n, total = (row or (0, 0, 0))
    return (yes_n or 0, no_n or 0, total or 0)


def _ensure_schema():
    conn = sqlite3.connect(str(LIP_DB), timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_skew_hint (
            ticker      TEXT PRIMARY KEY,
            skew_yes    REAL NOT NULL,
            confidence  REAL NOT NULL,
            reason      TEXT,
            computed_at TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skew_expires ON market_skew_hint(expires_at)")
    conn.commit()
    conn.close()


def _store_hints(predictions):
    conn = sqlite3.connect(str(LIP_DB), timeout=10)
    now = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=SKEW_TTL_MINUTES)).isoformat()
    for p in predictions:
        conn.execute(
            """INSERT INTO market_skew_hint(ticker, skew_yes, confidence, reason, computed_at, expires_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 skew_yes=excluded.skew_yes, confidence=excluded.confidence,
                 reason=excluded.reason, computed_at=excluded.computed_at,
                 expires_at=excluded.expires_at""",
            (p["ticker"], float(p["skew_yes"]), float(p.get("confidence", 0.5)),
             p.get("reason", "")[:200], now, expires),
        )
    # Garbage-collect stale
    conn.execute("DELETE FROM market_skew_hint WHERE expires_at < ?", (now,))
    conn.commit()
    conn.close()


def _kalshi_client():
    cwd = os.getcwd()
    os.chdir(str(LIP_PATH))
    sys.path.insert(0, str(LIP_PATH))
    from execution.kalshi_auth import KalshiClient
    c = KalshiClient()
    os.chdir(cwd)
    return c


def predict(markets_data, anthropic_key):
    """Single batched Haiku call. markets_data = list of enriched dicts.
    Returns parsed predictions list."""
    import anthropic
    client = anthropic.Anthropic(api_key=anthropic_key)
    user_msg = "Markets to score:\n" + json.dumps(markets_data, indent=2)
    r = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=2000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in r.content if hasattr(b, "text")).strip()
    # Strip code fences
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    parsed = json.loads(text)
    return parsed.get("predictions", []), {"in": r.usage.input_tokens, "out": r.usage.output_tokens}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=DEFAULT_N)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Try lip-maker .env
        env_file = LIP_PATH / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip("'\"")
                    break
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return 1

    _ensure_schema()

    markets = _get_top_markets(a.markets)
    if not markets:
        print("No active LIP markets found.")
        return 0

    c = _kalshi_client()
    enriched = []
    for m in markets:
        info = _enrich_market(c, m["ticker"])
        if not info or info.get("status") != "active":
            continue
        yes_n, no_n, total = _recent_fill_bias(m["ticker"], hours=2)
        enriched.append({
            "ticker":          m["ticker"],
            "title":           info["title"],
            "lip_per_day_usd": m["lip_per_day"],
            "mid_cents":       info["mid_c"],
            "yes_bid_cents":   info["yes_bid_c"],
            "yes_ask_cents":   info["yes_ask_c"],
            "vol_24h":         info["vol_24h"],
            "recent_fills":    {"yes": yes_n, "no": no_n, "total": total},
            "close_time":      info.get("close_time", "")[:10],
        })

    if not enriched:
        print("No enrichable markets.")
        return 0

    print(f"\n=== HYDRA-SKEW — predicting {len(enriched)} markets ===")
    try:
        predictions, usage = predict(enriched, api_key)
    except Exception as e:
        print(f"Prediction failed: {e}")
        return 1

    cost = usage["in"] * 1e-6 + usage["out"] * 5e-6   # Haiku pricing
    print(f"Got {len(predictions)} predictions (cost ${cost:.4f})\n")

    # Show non-neutral predictions
    nonneutral = [p for p in predictions if abs(float(p["skew_yes"]) - 0.5) > 0.02]
    print(f"  {'TICKER':<38} {'SKEW_YES':>9} {'CONF':>5} REASON")
    for p in sorted(nonneutral, key=lambda x: abs(float(x["skew_yes"]) - 0.5), reverse=True)[:25]:
        print(f"  {p['ticker'][:36]:<38} {float(p['skew_yes']):>8.2f} {float(p.get('confidence',0.5)):>5.2f} {(p.get('reason') or '')[:55]}")

    if not a.dry_run:
        _store_hints(predictions)
        print(f"\n✓ Stored {len(predictions)} hints (TTL {SKEW_TTL_MINUTES}min)")
    if a.json:
        print(json.dumps({"predictions": predictions, "cost_usd": cost}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
