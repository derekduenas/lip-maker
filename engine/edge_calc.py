"""Edge Calculator — combines frequency, context, and market price into trade signals."""

import argparse
import math
import sqlite3
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    DB_PATH, MIN_EDGE_SINGLE, MIN_BASE_RATE_N,
    KELLY_FRACTION, MAX_TRADE_PCT, FOMC_TERMS,
)
from engine.frequency import get_base_rate
from engine.context_scorer import score_term_context, get_recent_transcripts
from engine.scanner import KalshiClient, classify_market, SKIP_TERMS


def _get_conn():
    return sqlite3.connect(DB_PATH)


def kalshi_maker_fee(price: float) -> float:
    """
    Kalshi maker fee per contract.
    price: 0.0 to 1.0
    fee = 0.000175 + (0.004375 - 0.000175) * abs(2 * price - 1)
    """
    return 0.000175 + (0.004375 - 0.000175) * abs(2 * price - 1)


def kalshi_taker_fee(price: float) -> float:
    """Kalshi taker fee per contract (for reference — we never use this)."""
    return 0.0007 + (0.0175 - 0.0007) * abs(2 * price - 1)


def classify_trade_tier(estimated_prob: float, market_price: float,
                        corpus_n: int = 0) -> str:
    """
    Classify trade into tier based on Catboy playbook:
      'junk_bond':     price 70-90c, true_prob 95%+, small edge but safe
      'high_variance': price 10-60c, edge >= 15pp, large upside
      'mid_range':     everything else that clears edge threshold
      'skip':          outside viable zones
    """
    from config.settings import (
        JUNK_BOND_ENABLED, JUNK_BOND_MIN_PROB, JUNK_BOND_MAX_PRICE,
        JUNK_BOND_MIN_PRICE, JUNK_BOND_MIN_CORPUS_N,
        HV_TRADE_ENABLED, HV_TRADE_MIN_PRICE, HV_TRADE_MAX_PRICE,
    )

    # Junk bond: near-certain word at depressed price
    if (JUNK_BOND_ENABLED
            and estimated_prob >= JUNK_BOND_MIN_PROB
            and JUNK_BOND_MIN_PRICE <= market_price <= JUNK_BOND_MAX_PRICE
            and corpus_n >= JUNK_BOND_MIN_CORPUS_N):
        return "junk_bond"

    # High variance: mid-range price with big edge
    if (HV_TRADE_ENABLED
            and HV_TRADE_MIN_PRICE <= market_price <= HV_TRADE_MAX_PRICE):
        return "high_variance"

    # Mid-range
    if 0.05 < market_price < 0.95:
        return "mid_range"

    return "skip"


def confidence_weight(corpus_n: int = None) -> float:
    """Confidence multiplier based on corpus depth."""
    if corpus_n is None:
        return 0.6
    if corpus_n >= 20:
        return 1.0
    elif corpus_n >= 10:
        return 0.8
    elif corpus_n >= 5:
        return 0.6
    elif corpus_n >= 3:
        return 0.4
    return 0.0


def is_high_conviction(estimated_prob: float, market_price: float,
                       corpus_n: int, conf_weight: float) -> bool:
    """Detect high-conviction setups for aggressive sizing."""
    return (
        estimated_prob > 0.85
        and market_price < 0.65
        and corpus_n is not None and corpus_n >= 10
        and (estimated_prob - market_price) > 0.20
        and conf_weight >= 0.8
    )


def kelly_sizing(estimated_prob: float, price: float, corpus_n: int = None) -> tuple[float, float, bool, str]:
    """
    Kelly criterion with confidence weighting + high-conviction mode.
    Returns (deploy_pct, confidence, is_high_conv, strategy).
    """
    from config.settings import HIGH_CONVICTION_MAX_PCT

    if price <= 0 or price >= 1 or estimated_prob <= 0 or estimated_prob >= 1:
        return 0.0, 0.0, False, "skip"

    b = (1.0 - price) / price
    p = estimated_prob
    q = 1.0 - p

    f_star = (b * p - q) / b
    if f_star <= 0:
        return 0.0, 0.0, False, "skip"

    conf = confidence_weight(corpus_n)
    if conf == 0.0:
        return 0.0, 0.0, False, "skip"

    high_conv = is_high_conviction(estimated_prob, price, corpus_n, conf)

    if high_conv:
        # Full Kelly, higher cap
        deploy = f_star * conf
        cap = HIGH_CONVICTION_MAX_PCT
        strategy = "high_conviction"
    else:
        # Half-Kelly, normal cap
        deploy = f_star * KELLY_FRACTION * conf
        cap = MAX_TRADE_PCT
        strategy = "frequency_context"

    return min(deploy, cap), conf, high_conv, strategy


def calculate_edge(
    term: str,
    event_type: str,
    event_date: str,
    kalshi_ticker: str,
    yes_price: float,
    news_headlines: list[str] = None,
) -> dict | None:
    """
    Full pipeline:
    1. Get base_rate from frequency_matrix (return None if n < 5)
    2. Fetch recent transcripts for context
    3. Call context_scorer -> adjusted_prob
    4. Compute edge = adjusted_prob - yes_price - maker_fee (for YES)
       or edge = (1 - adjusted_prob) - (1 - yes_price) - maker_fee (for NO)
    5. Compute Kelly fraction
    6. Return full opportunity dict
    """
    # Step 0: Fetch resolution rules — use exact trigger phrase for matching
    search_term = term
    is_rolling = False
    try:
        from engine.rules import extract_trigger_phrase, fetch_and_cache_rules
        from engine.scanner import KalshiClient
        client = KalshiClient()
        if client.authenticated:
            rules = fetch_and_cache_rules(client, kalshi_ticker)
            if rules.get("variants"):
                search_term = rules["variants"][0]  # use exact trigger phrase
            is_rolling = rules.get("is_rolling_window", False)
    except Exception:
        pass  # fallback to raw term

    # Skip rolling-window markets — our corpus models single events, not windows
    if is_rolling:
        return {
            "ticker": kalshi_ticker, "term": term, "signal": "SKIP",
            "reasoning": "Rolling window market — corpus models single events only",
            "base_rate": None, "context_delta": 0.0, "estimated_prob": None,
            "market_price": yes_price, "edge": 0.0, "kelly_fraction": 0.0,
            "deploy_pct": 0.0, "trade_side": None, "confidence": "none",
        }

    # Step 1: Get base rate — use rules-derived search term
    base_rate = get_base_rate(event_type, search_term)
    if base_rate is None and search_term != term:
        base_rate = get_base_rate(event_type, term)  # fallback to raw term

    # Universal Bayesian Laplace smoothing.
    # Apply when base_rate is None (retrieval fallback) OR when base_rate is
    # degenerate 0.0/1.0 (term never/always seen in corpus — leads to
    # overconfident bets like Trump saying "drill baby drill" with p=0.02%).
    # "Never seen in corpus" is not "impossible"; smoothing enforces epistemic humility.
    if base_rate is None or base_rate == 0.0 or base_rate == 1.0:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT base_rate, occurrences, total_events FROM frequency_matrix WHERE event_type=? AND term=?",
            (event_type, term)
        ).fetchone()
        conn.close()
        if row and row[2] >= 3:  # minimum corpus depth for smoothed estimate
            k, n = row[1], row[2]
            base_rate = (k + 1) / (n + 2)  # Bayesian smoothed

    if base_rate is None:
        return {
            "ticker": kalshi_ticker,
            "term": term,
            "signal": "SKIP",
            "reasoning": f"Insufficient data (n < {MIN_BASE_RATE_N})",
            "base_rate": None,
            "context_delta": 0.0,
            "estimated_prob": None,
            "market_price": yes_price,
            "edge": 0.0,
            "kelly_fraction": 0.0,
            "deploy_pct": 0.0,
            "trade_side": None,
            "confidence": "none",
        }

    # Step 2: Fetch recent transcripts
    recent = get_recent_transcripts(event_type, term)

    # Step 3: Context scoring
    if news_headlines is None:
        news_headlines = []

    context = score_term_context(
        term=term,
        event_type=event_type,
        event_date=event_date,
        base_rate=base_rate,
        recent_transcripts=recent,
        news_headlines=news_headlines,
    )

    estimated_prob = context["adjusted_prob"]
    context_delta = context["context_delta"]
    reasoning = context["reasoning"]
    confidence = context["confidence"]

    # Step 4: Compute edge for both sides
    fee_yes = kalshi_maker_fee(yes_price)
    fee_no = kalshi_maker_fee(1 - yes_price)

    edge_yes = estimated_prob - yes_price - fee_yes
    edge_no = (1 - estimated_prob) - (1 - yes_price) - fee_no

    # Get corpus depth for confidence weighting
    import sqlite3 as _sql
    _conn = _sql.connect(DB_PATH)
    _corpus_n = _conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchone()[0]
    _conn.close()

    # Pick the better side
    if edge_yes >= edge_no and edge_yes > 0:
        edge = edge_yes
        trade_side = "yes"
        kelly_pct, conf, high_conv, strategy = kelly_sizing(estimated_prob, yes_price, corpus_n=_corpus_n)
    elif edge_no > 0:
        edge = edge_no
        trade_side = "no"
        kelly_pct, conf, high_conv, strategy = kelly_sizing(1 - estimated_prob, 1 - yes_price, corpus_n=_corpus_n)
    else:
        edge = max(edge_yes, edge_no)
        trade_side = "yes" if edge_yes >= edge_no else "no"
        kelly_pct, conf, high_conv, strategy = 0.0, 0.0, False, "skip"

    # Dual confirmation rule: corpus AND context must agree
    corpus_direction = "yes" if base_rate > yes_price else "no"
    context_direction = "yes" if context_delta > 0 else ("no" if context_delta < 0 else "neutral")
    dual_confirmed = True

    if trade_side != corpus_direction and edge < 0.25:
        # Corpus and trade direction disagree — skip unless massive edge
        dual_confirmed = False

    if context_direction != "neutral" and context_direction != trade_side and confidence in ("medium", "high"):
        # Claude context actively contradicts the trade direction
        dual_confirmed = False

    # Classify trade tier
    from config.settings import (
        JUNK_BOND_ENABLED, JUNK_BOND_MIN_EDGE, JUNK_BOND_KELLY_MULTIPLIER,
        HV_TRADE_MIN_EDGE, HV_TRADE_KELLY_MULTIPLIER,
    )

    trade_tier = classify_trade_tier(estimated_prob, yes_price, _corpus_n)

    # Tier-specific edge thresholds and Kelly adjustments
    if trade_tier == "junk_bond":
        min_edge_for_tier = JUNK_BOND_MIN_EDGE
        kelly_pct *= JUNK_BOND_KELLY_MULTIPLIER
        strategy = "junk_bond"
        # Junk bonds skip dual confirmation — corpus alone is sufficient at 95%+
        dual_confirmed = True
    elif trade_tier == "high_variance":
        min_edge_for_tier = HV_TRADE_MIN_EDGE
        kelly_pct *= HV_TRADE_KELLY_MULTIPLIER
        strategy = "high_variance"
    else:
        min_edge_for_tier = MIN_EDGE_SINGLE

    # Extreme-market skepticism (Lead Alpha audit 2026-04-19).
    # When the market prices one side very confidently and the corpus has
    # WEAKER conviction in the opposite direction, the market likely has
    # live event-specific information the corpus cannot see. Verified against
    # walkforward losses:
    #   - "trillion" NO @ yes_price 0.95 with corpus_YES 0.76 → blocked (Trump
    #      said it; corpus 76% was averaged across 2024-2026, market knew 95%+)
    #   - "barack hussein obama" YES @ yes_price 0.12 with corpus 0.28 → blocked
    # Both cases the bet was against a confident market with corpus not strongly
    # supporting our side. Blocking converts 2/3 walkforward losses to skips.
    extreme_skip = False
    extreme_reason = ""
    if trade_side == "no" and yes_price > 0.85 and base_rate >= 0.50:
        extreme_skip = True
        extreme_reason = f"extreme_market: NO against {yes_price:.2f} YES market, corpus={base_rate:.2f} not NO-leaning"
    elif trade_side == "yes" and yes_price < 0.15 and base_rate <= 0.50:
        extreme_skip = True
        extreme_reason = f"extreme_market: YES against {yes_price:.2f} YES market, corpus={base_rate:.2f} not YES-leaning"

    # Determine signal
    if extreme_skip:
        signal = "SKIP"
        reasoning = (reasoning + "; " + extreme_reason) if reasoning else extreme_reason
    elif edge >= min_edge_for_tier and dual_confirmed:
        signal = f"BUY_{trade_side.upper()}"
    elif edge >= min_edge_for_tier and not dual_confirmed:
        signal = "SKIP"
    else:
        signal = "SKIP"

    return {
        "ticker": kalshi_ticker,
        "term": term,
        "base_rate": base_rate,
        "context_delta": context_delta,
        "estimated_prob": estimated_prob,
        "market_price": yes_price,
        "edge": edge,
        "kelly_fraction": kelly_pct,
        "deploy_pct": kelly_pct,
        "signal": signal,
        "trade_tier": trade_tier,
        "reasoning": reasoning,
        "trade_side": trade_side,
        "confidence": confidence,
        "confidence_weight": conf,
        "high_conviction": high_conv,
        "strategy": strategy,
        "corpus_n": _corpus_n,
    }


def _log_opportunity(opp: dict) -> None:
    """Insert qualifying opportunity into the DB."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO opportunities
           (kalshi_market_id, term, base_rate, context_score, estimated_prob,
            market_price, edge, kelly_fraction, parlay_flag)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            opp["ticker"],
            opp["term"],
            opp.get("base_rate"),
            opp.get("context_delta"),
            opp["estimated_prob"],
            opp["market_price"],
            opp["edge"],
            opp["kelly_fraction"],
            False,
        )
    )
    conn.commit()
    conn.close()


def run_full_scan(event_type: str, news_headlines: list[str] = None) -> list[dict]:
    """
    Polls all open Kalshi mention markets of event_type.
    Runs calculate_edge for each.
    Inserts qualifying opportunities into opportunities table.
    Prints edge report.
    Returns list of all edge results.
    """
    client = KalshiClient()
    markets = client.poll_all_mention_markets()

    # Filter to requested event type
    results = []
    qualifying = 0

    for market in markets:
        classified = classify_market(market)
        if classified["event_type"] != event_type:
            continue

        term = classified.get("term")
        if not term:
            continue

        # Skip terms from the no-trade list
        if term.lower() in [s.lower() for s in SKIP_TERMS]:
            # Still compute but don't flag as trade
            pass

        event_date = classified.get("event_date", "unknown")
        yes_price = market.get("yes_price", 0.5)

        edge_result = calculate_edge(
            term=term,
            event_type=event_type,
            event_date=event_date,
            kalshi_ticker=market["ticker"],
            yes_price=yes_price,
            news_headlines=news_headlines,
        )

        if edge_result:
            results.append(edge_result)
            if edge_result["signal"] != "SKIP" and edge_result.get("estimated_prob") is not None:
                _log_opportunity(edge_result)
                qualifying += 1

    # Print report
    _print_edge_report(event_type, results, qualifying)
    return results


def _print_edge_report(event_type: str, results: list[dict], qualifying: int) -> None:
    """Print formatted edge report per CLAUDE.md spec."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    event_dates = set(r.get("ticker", "").split("-")[-1] for r in results if r.get("ticker"))

    label = event_type.upper().replace("_", " ")
    print(f"\n{'=' * 72}")
    print(f"SOVEREIGN EDGE REPORT — {label} — {now}")
    print(f"{'=' * 72}")
    print(f"Scanned {len(results)} open {label} mention markets on Kalshi")
    print(f"Qualifying opportunities: {qualifying}")
    print()

    # Sort: signals first, then by edge desc
    sorted_results = sorted(results, key=lambda r: (
        0 if r["signal"] != "SKIP" else 1,
        -abs(r.get("edge", 0))
    ))

    # Table header
    print(f"{'Term':<22} | {'Base Rate':>9} | {'Adj Prob':>8} | {'Mkt Price':>9} | {'Edge':>7} | {'Signal':<12}")
    print("-" * 80)

    for r in sorted_results:
        base = f"{r['base_rate']*100:.1f}%" if r.get('base_rate') is not None else "  N/A"
        adj = f"{r['estimated_prob']*100:.1f}%" if r.get('estimated_prob') is not None else "  N/A"
        mkt = f"{r['market_price']*100:.0f}c"
        edge = f"{r['edge']*100:+.1f}pp" if r.get('edge') else " 0.0pp"

        if r["signal"] == "SKIP":
            sig = "SKIP"
        elif "YES" in r["signal"]:
            sig = "BUY YES *"
        else:
            sig = "BUY NO  *"

        print(f"{r['term']:<22} | {base:>9} | {adj:>8} | {mkt:>9} | {edge:>7} | {sig:<12}")

    print("-" * 80)

    # Kelly sizing for qualifying trades
    qualifying_trades = [r for r in sorted_results if r["signal"] != "SKIP"]
    if qualifying_trades:
        print("\nKelly sizing (half-Kelly, 15% cap):")
        for r in qualifying_trades:
            side = r["trade_side"].upper()
            if side == "YES":
                price_display = f"{r['market_price']*100:.0f}c"
            else:
                price_display = f"{(1-r['market_price'])*100:.0f}c"
            deploy = f"{r['deploy_pct']*100:.1f}%"
            print(f"  {r['term']:<22} {side} @{price_display} -> {deploy} of bankroll")

    print(f"\n[PAPER MODE — No orders placed]")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Edge Calculator")
    parser.add_argument("--scan", type=str, help="Run full scan for event_type (e.g., 'fomc')")
    args = parser.parse_args()

    event_type = "fomc_presser"
    if args.scan:
        # Map shorthand to full event_type
        scan_map = {
            "fomc": "fomc_presser",
            "fomc_presser": "fomc_presser",
            "tsla": "tsla_earnings",
            "trump": "trump_speech",
        }
        event_type = scan_map.get(args.scan, args.scan)

    # Use some representative current headlines for context
    headlines = [
        "Fed holds rates steady amid trade uncertainty — Reuters",
        "Tariffs drive inflation concerns as consumer prices tick up — WSJ",
        "Powell signals patience, says economy resilient — Bloomberg",
        "Labor market shows signs of cooling in latest jobs report — CNBC",
        "Markets brace for FOMC decision as stagflation fears grow — FT",
        "CPI data shows continued disinflation trend in services — AP",
        "Trade policy uncertainty weighs on business investment — NYT",
        "Housing market softens as mortgage rates remain elevated — MarketWatch",
    ]

    results = run_full_scan(event_type, news_headlines=headlines)
