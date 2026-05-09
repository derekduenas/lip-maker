"""Pre-Live Audit — comprehensive system check before deploying real capital."""

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH


def run_audit() -> dict:
    """Run all pre-live checks. Returns pass/fail/warn counts."""
    results = []

    def check(category, name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        results.append({"category": category, "name": name, "status": status, "detail": detail})
        icon = "OK" if passed else "XX"
        print(f"  [{icon}] {name}")
        if detail and not passed:
            print(f"       {detail}")

    print("=" * 55)
    print("PRE-LIVE AUDIT REPORT")
    print("=" * 55)

    # ── 1. API CONNECTIVITY ──
    print("\n1. API CONNECTIVITY")
    try:
        from engine.scanner import KalshiClient
        client = KalshiClient()
        check("api", "Kalshi API key loaded", client.authenticated)
        check("api", "Kalshi private key loaded", client._private_key is not None)

        if client.authenticated:
            data = client._get("/exchange/status")
            check("api", "Kalshi exchange reachable", data.get("exchange_active", False))

            bal = client._get("/portfolio/balance")
            balance = float(bal.get("balance", 0)) / 100.0
            check("api", f"Kalshi balance: ${balance:.2f}", balance > 0,
                  f"Balance too low" if balance <= 0 else "")
        else:
            check("api", "Kalshi exchange reachable", False, "Not authenticated")
            check("api", "Kalshi balance check", False, "Not authenticated")
    except Exception as e:
        check("api", "Kalshi API connection", False, str(e))

    try:
        from engine.context_scorer import _get_anthropic_client
        ac = _get_anthropic_client()
        check("api", "Anthropic API key loaded", ac is not None)
    except Exception as e:
        check("api", "Anthropic API", False, str(e))

    # ── 2. DATABASE INTEGRITY ──
    print("\n2. DATABASE INTEGRITY")
    conn = sqlite3.connect(DB_PATH)
    expected_tables = ["transcripts", "mentions", "frequency_matrix", "cooccurrence",
                       "market_snapshots", "opportunities", "trades", "strategy_scores",
                       "ingestion_log", "transcript_quality", "shadow_trades"]
    actual = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for t in expected_tables:
        check("db", f"Table exists: {t}", t in actual)

    transcript_count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    check("db", f"Transcripts: {transcript_count}", transcript_count >= 100,
          f"Only {transcript_count} — need 100+" if transcript_count < 100 else "")

    fm_count = conn.execute("SELECT COUNT(*) FROM frequency_matrix").fetchone()[0]
    check("db", f"Frequency entries: {fm_count}", fm_count >= 100)

    # ── 3. SAFETY RAILS ──
    print("\n3. SAFETY RAILS")
    from config.settings import (PAPER_MODE, SHADOW_MODE, MAX_TRADE_PCT,
                                  MAX_DAILY_DEPLOY, MAX_DAILY_TRADES, MAX_TRADES_PER_EVENT,
                                  MIN_MARKET_PRICE, MAX_MARKET_PRICE, MIN_MARKET_VOLUME)
    check("safety", f"MAX_TRADE_PCT = {MAX_TRADE_PCT*100:.0f}%", MAX_TRADE_PCT <= 0.20)
    check("safety", f"MAX_DAILY_DEPLOY = {MAX_DAILY_DEPLOY*100:.0f}%", MAX_DAILY_DEPLOY <= 0.50)
    check("safety", f"MAX_DAILY_TRADES = {MAX_DAILY_TRADES}", MAX_DAILY_TRADES <= 10)
    check("safety", f"Price filter: {MIN_MARKET_PRICE*100:.0f}c-{MAX_MARKET_PRICE*100:.0f}c", True)
    check("safety", f"Volume filter: ${MIN_MARKET_VOLUME}", MIN_MARKET_VOLUME >= 100)
    check("safety", f"PAPER_MODE = {PAPER_MODE}", True)  # just report current state
    check("safety", f"SHADOW_MODE = {SHADOW_MODE}", True)

    # ── 4. CORPUS FRESHNESS ──
    print("\n4. CORPUS FRESHNESS")
    for et in ["fomc_presser", "fomc_minutes", "fomc_statement"]:
        n = conn.execute("SELECT COUNT(*) FROM transcripts WHERE event_type=?", (et,)).fetchone()[0]
        latest = conn.execute("SELECT MAX(event_date) FROM transcripts WHERE event_type=?", (et,)).fetchone()[0]
        check("corpus", f"{et}: n={n}, latest={latest}", n >= 10)

    # ── 5. RULES PARSER ──
    print("\n5. RULES PARSER")
    from engine.rules import extract_trigger_phrase
    test_rules = [
        ('If "inflation" is said by any representative', "inflation"),
        ('If Drill Baby Drill, or a plural form, is stated by Trump', "drill baby drill"),
        ('If Ad-Supported is said during the earnings call', "ad-supported"),
    ]
    for rule_text, expected in test_rules:
        parsed = extract_trigger_phrase(rule_text)
        found = parsed.get("trigger_phrase", "")
        check("rules", f"Parse: '{expected}'",
              found and expected in found,
              f"Got: '{found}'" if found != expected else "")

    # ── 6. EXECUTION PATH ──
    print("\n6. EXECUTION PATH")
    from engine.executor import Executor
    ex = Executor()
    check("exec", f"Executor bankroll: ${ex.bankroll:.2f}", ex.bankroll > 0)
    check("exec", f"Paper mode: {ex.paper}", True)

    contracts = ex.size_position(kelly_fraction=0.10, price=0.50)
    check("exec", f"Position sizing works: {contracts} contracts", contracts > 0)

    # ── 7. ENGINE CONFIG ──
    print("\n7. ENGINE CONFIG")
    from config.engines import get_active_engines
    engines = get_active_engines()
    check("engines", f"Active engines: {len(engines)}", len(engines) >= 2)
    for name, eng in engines.items():
        check("engines", f"  {name}: {eng['status']}", eng["status"] in ("ACTIVE", "SECONDARY"))

    # ── 8. TRADE HISTORY ──
    print("\n8. TRADE HISTORY")
    wins = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='WIN'").fetchone()[0]
    losses = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='LOSS'").fetchone()[0]
    total = wins + losses
    wr = wins / total * 100 if total > 0 else 0
    # Tightened 2026-04-19: require real sample + real WR (no bypass on total<5).
    # Old gate was "total>=3" + "WR>=60% or total<5" — too lenient; a single-trade
    # paper history could pass. Live capital shouldn't ship on that.
    check("history", f"Resolved trades: {total} ({wins}W/{losses}L)", total >= 20,
          f"Need 20+ resolved paper trades before live promotion" if total < 20 else "")
    check("history", f"Win rate: {wr:.0f}%", total >= 20 and wr >= 65,
          f"Need ≥65% WR on ≥20 paper trades" if not (total >= 20 and wr >= 65) else "")

    pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE outcome IN ('WIN','LOSS')").fetchone()[0]
    check("history", f"Paper PnL: ${pnl:+.2f}", pnl > 0)

    # Per-event-type WR — added 2026-04-19 after walkforward showed Trump
    # speeches losing (2W/3L) while FDX earnings winning (2W/0L). A single
    # losing event_type can drag the overall winners down when scaled live.
    # (Group by ticker prefix since trades table doesn't have event_type col.)
    evt_rows = conn.execute("""
        SELECT
            CASE WHEN kalshi_market_id LIKE '%FOMC%' THEN 'fomc'
                 WHEN kalshi_market_id LIKE '%EARNINGSMENTION%' OR kalshi_market_id LIKE '%MENTIONEARN%' THEN 'earnings'
                 WHEN kalshi_market_id LIKE '%TRUMP%' THEN 'trump_speech'
                 ELSE 'other' END as evt,
            COUNT(*) as n,
            SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as w,
            SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as l
          FROM trades
         WHERE outcome IN ('WIN','LOSS')
         GROUP BY evt
    """).fetchall()
    if evt_rows:
        print("   per-event-type breakdown:")
        for evt, n, w, l in evt_rows:
            ewr = (w / (w + l) * 100) if (w + l) else 0
            print(f"     {evt:20s}: {w}W/{l}L  WR={ewr:.0f}%")
            if (w + l) >= 5:
                check("history", f"  {evt} WR≥50%", ewr >= 50,
                      f"event_type '{evt}' loses on 5+ samples")

    # ── 9. WALKFORWARD VALIDATION ──
    print("\n9. WALKFORWARD VALIDATION")
    import json as _json
    from pathlib import Path as _Path
    wf_path = _Path(DB_PATH).parent / "walkforward_results.json"
    try:
        with open(wf_path) as f:
            wf = _json.load(f)
        wf_trades = wf.get("total_trades", 0)
        wf_wr = wf.get("win_rate", 0)
        wf_roi = wf.get("roi", -999)
        wf_ts = wf.get("timestamp", "?")
        print(f"   walkforward timestamp: {wf_ts}")
        check("walkforward", f"Walkforward sample: {wf_trades} trades", wf_trades >= 14,
              f"Need ≥14 OOS trades (have {wf_trades})" if wf_trades < 14 else "")
        check("walkforward", f"Walkforward WR: {wf_wr:.1f}%", wf_wr >= 55.0,
              f"OOS WR below 55% threshold" if wf_wr < 55 else "")
        check("walkforward", f"Walkforward ROI: {wf_roi:.2f}%", wf_roi > 0,
              f"OOS ROI must be positive before live" if wf_roi <= 0 else "")
    except FileNotFoundError:
        check("walkforward", "Walkforward results file exists", False,
              "Run tools/walkforward.py before requesting live promotion")
    except Exception as e:
        check("walkforward", "Walkforward results parseable", False, str(e))

    conn.close()

    # ── SUMMARY ──
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    total_checks = len(results)

    print()
    print("=" * 55)
    print(f"PASSED:  {passed} / {total_checks}")
    print(f"FAILED:  {failed}")

    if failed == 0:
        print(f"\nREADY FOR LIVE: YES")
        print("Flip PAPER_MODE=False and SHADOW_MODE=False in .env")
        print("Restart service. Next 6am scan goes live.")
    elif failed <= 3:
        print(f"\nREADY FOR LIVE: INVESTIGATE")
        print("Fix the failed checks, re-run audit.")
    else:
        print(f"\nREADY FOR LIVE: NO")
        print("Too many failures. Address before deploying capital.")

    print("=" * 55)
    return {"passed": passed, "failed": failed, "total": total_checks, "results": results}


if __name__ == "__main__":
    run_audit()
