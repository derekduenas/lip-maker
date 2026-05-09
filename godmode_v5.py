"""SOVEREIGN v5 GOD-MODE — calibrated trading rules.

What v5 changes vs v3/v4 (based on May 7 post-mortem of 1W/8L):
  ADDS MIN_K_W = 1.5 hard gate    — kills "pure Laplace prior" bets (5/8 losses)
  ADDS Wilson confidence shrinkage — uses lower-bound for YES, upper-bound for NO
                                     (small-N corpus → conservative prior)
  ADDS Pre-registration log        — hash predictions BEFORE event = no backfit
  ADDS MIN_MISPRICING = 0.20       — only bet if empirical-vs-market gap >= 20pp
                                     (was 5pp; too loose for thin corpus)
"""
from __future__ import annotations
import argparse, hashlib, json, math, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

SOV = Path("/root/sovereign")
sys.path.insert(0, str(SOV))

# Re-use v3 functions
from earnings_pipeline_v3 import (
    compute_base_rate_recency_weighted,
    fee_dollars,
    passes_liquidity_filter,
    passes_spread_filter,
    passes_settle_window_filter,
)

DB = SOV / "data" / "sovereign.db"
BANKROLL = 1000.0

# v5 GATES (post-mortem-derived)
MIN_K_W              = 1.5    # require real signal; was: none. Kills 5/8 losses.
MIN_N_RAW            = 4      # corpus depth (kept at 4 now; raise to 6+ when corpus grows)
MIN_MISPRICING_PP    = 0.20   # 20pp gap empirical-vs-market; was 5pp.
MAX_KELLY_FRACTION   = 0.10   # quarter-Kelly is too aggressive for thin priors. 1/10 Kelly.
PER_EVENT_CAP_USD    = 400.0
PER_TRADE_CAP_USD    = 50.0


def wilson_ci(k: float, n: float, z: float = 1.96):
    """Wilson score 95% CI for binomial. Use weighted k, n.
    Returns (lower, upper). For YES bets use LOWER (conservative).
    For NO bets use 1 - UPPER (also conservative on NO probability)."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def kelly_size_v5(prob_for_side: float, price: float, bankroll: float,
                  cap: float = PER_TRADE_CAP_USD) -> dict:
    """1/10 Kelly with hard $cap. Conservative for thin-corpus regime."""
    if price <= 0 or price >= 1:
        return {"contracts": 0, "stake_usd": 0.0, "f": 0.0}
    edge = prob_for_side - price - fee_dollars(price)
    if edge <= 0:
        return {"contracts": 0, "stake_usd": 0.0, "f": 0.0}
    b = (1.0 - price) / price  # binary payoff
    p = prob_for_side
    q = 1.0 - p
    f_kelly = (b * p - q) / b if b > 0 else 0
    f = max(0.0, min(MAX_KELLY_FRACTION, f_kelly * 0.10))  # 1/10 Kelly
    stake = min(cap, f * bankroll)
    contracts = int(stake / max(price, 0.01))
    return {"contracts": contracts, "stake_usd": round(contracts * price, 2),
            "f": round(f, 4), "edge": round(edge, 4)}


def score_event_v5(ticker: str) -> tuple[list[dict], dict, dict]:
    """v5 god-mode scorer. Returns (opportunities, funnel_counts, drops)."""
    from engine.scanner import KalshiClient
    c = KalshiClient()
    series_pfx = f"KXEARNINGSMENTION{ticker.upper()}"
    all_mkts = c.get_mention_markets()
    target = [m for m in all_mkts if m.get("ticker", "").startswith(series_pfx)]

    funnel = {
        "L1_target_match":        len(target),
        "L2_liquidity":           0,
        "L3_spread":              0,
        "L4_settle_window":       0,
        "L5_corpus_real_signal":  0,  # NEW: requires k_w >= 1.5
        "L6_mispricing_20pp":     0,  # NEW: requires 20pp gap
        "L7_kelly_sized":         0,
    }
    drops = {k: [] for k in [
        "liquidity", "spread", "settle_window",
        "no_corpus", "thin_corpus_kw", "no_mispricing",
        "kelly_zero",
    ]}
    out = []

    for m in target:
        tk = m["ticker"]
        ok, why = passes_liquidity_filter(m)
        if not ok:
            drops["liquidity"].append((tk, why)); continue
        funnel["L2_liquidity"] += 1

        ok, why = passes_spread_filter(m)
        if not ok:
            drops["spread"].append((tk, why)); continue
        funnel["L3_spread"] += 1

        ok, why = passes_settle_window_filter(m)
        if not ok:
            drops["settle_window"].append((tk, why)); continue
        funnel["L4_settle_window"] += 1

        sub = m.get("subtitle") or m.get("yes_sub_title") or ""
        if not sub:
            drops["no_corpus"].append((tk, "no_term")); continue
        br = compute_base_rate_recency_weighted(ticker, sub)
        if br is None:
            drops["no_corpus"].append((tk, "no_transcripts")); continue

        # v5 GATE: real signal required (was: any base rate accepted)
        if br["k_w"] < MIN_K_W:
            drops["thin_corpus_kw"].append(
                (tk, f"k_w={br['k_w']:.2f}<{MIN_K_W} (Laplace-only)")
            )
            continue
        funnel["L5_corpus_real_signal"] += 1

        # v5: Wilson confidence shrinkage
        lo, hi = wilson_ci(br["k_w"], br["n_w"])
        # Use POINT estimate for direction, but SHRUNKEN value for sizing
        p_point = br["prob"]
        p_yes_conservative = lo  # lower bound of YES = conservative for YES bets
        p_no_conservative = 1 - hi  # 1 - upper bound = conservative for NO bets

        ya = m.get("yes_ask")
        na = m.get("no_ask") or m.get("no_price")
        try:
            ya = float(ya) if ya is not None else None
            na = float(na) if na is not None else None
        except Exception:
            ya = na = None
        if ya is None and na is None:
            drops["no_mispricing"].append((tk, "no_prices")); continue

        # v5 MISPRICING-DIRECTION rule:
        # market_implied_yes = ya (price of YES = market's prob estimate)
        # If empirical >> market_yes by 20pp+: bet YES (market under-prices)
        # If market_yes >> empirical by 20pp+: bet NO (market over-prices)
        market_implied_yes = ya if ya is not None else (1 - na)
        gap = p_point - market_implied_yes  # positive = empirical higher

        if abs(gap) < MIN_MISPRICING_PP:
            drops["no_mispricing"].append(
                (tk, f"|empirical-market|={abs(gap)*100:.1f}pp<{MIN_MISPRICING_PP*100:.0f}pp")
            )
            continue
        funnel["L6_mispricing_20pp"] += 1

        if gap > 0:
            # market under-prices YES → bet YES, use shrunken-low for size
            side, price, prob_for_size = "YES", ya, p_yes_conservative
        else:
            # market over-prices YES → bet NO
            side, price, prob_for_size = "NO", na, p_no_conservative

        if price is None or price <= 0:
            drops["no_mispricing"].append((tk, f"side={side}_no_price")); continue

        sizing = kelly_size_v5(prob_for_size, price, BANKROLL)
        if sizing["contracts"] < 1:
            drops["kelly_zero"].append((tk, "f_zero")); continue
        funnel["L7_kelly_sized"] += 1

        out.append({
            "ticker_kalshi":  tk,
            "term":           sub,
            "rules":          (m.get("rules_primary") or "")[:200],
            "base_rate":      round(p_point, 4),
            "wilson_lo":      round(lo, 4),
            "wilson_hi":      round(hi, 4),
            "k_w":            br["k_w"],
            "n_w":            br["n_w"],
            "n_raw":          br["n_raw"],
            "yes_ask":        ya,
            "no_ask":         na,
            "vol_24h":        float(m.get("volume") or m.get("volume_24h_fp") or 0),
            "side":           side,
            "price":          price,
            "mispricing_pp":  round(gap * 100, 2),
            "prob_for_size":  round(prob_for_size, 4),
            **sizing,
        })

    out.sort(key=lambda x: -abs(x["mispricing_pp"]))
    return out, funnel, drops


def pre_register(opportunities: list[dict], event_id: str) -> str:
    """Hash all predictions BEFORE settlement. Prevents backfit."""
    payload = {
        "event_id": event_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "predictions": [
            {
                "ticker": o["ticker_kalshi"],
                "side":   o["side"],
                "price":  o["price"],
                "prob":   o["base_rate"],
                "wilson": [o["wilson_lo"], o["wilson_hi"]],
                "edge":   o.get("edge"),
                "size":   o["contracts"],
            }
            for o in opportunities
        ],
    }
    text = json.dumps(payload, sort_keys=True)
    h = hashlib.sha256(text.encode()).hexdigest()[:16]

    out_dir = SOV / "data" / "pre_register"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{event_id}_{h}.json"
    with open(out_file, "w") as f:
        json.dump({"hash": h, **payload}, f, indent=2)

    print(f"\n🔒 PRE-REGISTERED: {out_file}\n   hash: {h}\n   {len(opportunities)} predictions")
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True, help="e.g. MCD, LYFT, WMT")
    ap.add_argument("--event-id", help="e.g. earnings_may21_2026 (default: auto)")
    ap.add_argument("--no-register", action="store_true", help="skip pre-registration")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    event_id = a.event_id or f"earnings_{a.ticker}_{datetime.now().strftime('%Y%m%d')}"
    opps, funnel, drops = score_event_v5(a.ticker)

    if a.json:
        print(json.dumps({"funnel": funnel, "drops": drops, "opportunities": opps}, indent=2))
    else:
        print(f"\n=== SOVEREIGN v5 GOD-MODE — {a.ticker} ===")
        print(f"Funnel: {funnel}")
        if drops["thin_corpus_kw"]:
            print(f"\n  Killed by k_w<{MIN_K_W} gate (would-be Laplace bets):")
            for tk, why in drops["thin_corpus_kw"][:10]:
                print(f"    {tk}: {why}")
        if drops["no_mispricing"]:
            print(f"\n  Killed by mispricing<{MIN_MISPRICING_PP*100:.0f}pp gate:")
            for tk, why in drops["no_mispricing"][:10]:
                print(f"    {tk}: {why}")
        print(f"\n  PASSED ({len(opps)} opportunities):")
        for o in opps[:20]:
            print(
                f"    {o['ticker_kalshi'][:48]:48} {o['side']:3} @ {o['price']*100:.0f}c  "
                f"prob={o['base_rate']*100:.1f}%  wilson=[{o['wilson_lo']*100:.0f},{o['wilson_hi']*100:.0f}]  "
                f"mispr={o['mispricing_pp']:+.1f}pp  qty={o['contracts']}  ${o['stake_usd']:.2f}"
            )

    if opps and not a.no_register:
        pre_register(opps, event_id)


if __name__ == "__main__":
    main()
