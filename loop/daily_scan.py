"""Daily Scan Engine — proactive autonomous scanning on a fixed schedule.

Replaces the reactive event-triggered model with a daily schedule:
  6:00 AM  — morning_scan: poll ALL open mention markets, find edge, trade
  12:00 PM — midday_check: check for surprise resolutions
  8:00 PM  — evening_review: resolve trades, score strategies, extract lessons
"""

import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

import schedule

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, PAPER_MODE, MIN_EDGE_SINGLE

# Setup logging
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "sovereign.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sovereign")

NO_CORPUS_LOG = os.path.join(LOG_DIR, "no_corpus.log")


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _has_corpus(event_type: str) -> bool:
    """Check if we have any transcripts for this event type."""
    conn = _get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchone()[0]
    conn.close()
    return count >= 3  # minimum for earnings (Bayesian smoothing needs at least 3)


def morning_scan() -> dict:
    """
    Daily 6:00 AM scan. Polls ALL open Kalshi mention markets,
    filters to those resolving within 72h, computes edge, trades.
    """
    log.info("=== MORNING SCAN START ===")

    from engine.scanner import KalshiClient, classify_market
    from engine.edge_calc import calculate_edge, _log_opportunity
    from engine.executor import Executor
    from config.watchlist import add_to_watchlist
    from config.engines import get_engine_for_event, get_active_engines

    client = KalshiClient()
    executor = Executor()
    active_engines = get_active_engines()
    log.info(f"Active engines: {', '.join(active_engines.keys())}")
    now = datetime.utcnow()
    cutoff = now + timedelta(hours=72)

    summary = {
        "timestamp": now.isoformat(),
        "scanned": 0,
        "qualified": 0,
        "opportunities": 0,
        "trades_placed": 0,
        "skipped_no_corpus": 0,
    }

    # 1. Poll ALL open mention markets
    log.info("Polling all open Kalshi mention markets...")
    markets = client.get_mention_markets()
    summary["scanned"] = len(markets)
    log.info(f"Found {len(markets)} open mention markets")

    # 2. Classify and filter
    candidates = []
    for market in markets:
        classified = classify_market(market)
        event_date_str = classified.get("event_date")

        # Filter: must have a date, must resolve within 72h
        if not event_date_str:
            continue
        try:
            event_dt = datetime.strptime(event_date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if event_dt > cutoff or event_dt < now - timedelta(hours=24):
            continue

        candidates.append((market, classified))

    summary["qualified"] = len(candidates)
    log.info(f"Qualified (resolving within 72h): {len(candidates)} markets")

    # 3. Fetch real news context (once per scan, not per market)
    try:
        from engine.news import get_macro_context, get_earnings_context
        headlines = get_macro_context()
        log.info(f"Fetched {len(headlines)} macro headlines for context")
    except Exception as e:
        log.warning(f"News fetch failed, using fallback: {e}")
        headlines = []

    # Cache per-ticker headlines to avoid re-fetching
    ticker_headlines_cache = {}

    # Track tickers that already failed auto-ingest this cycle — don't retry
    ingest_failed_this_cycle = set()
    opportunity_queue = []

    for market, classified in candidates:
        event_type = classified["event_type"]
        term = classified.get("term")
        ticker_code = classified.get("event_type", "").split("_")[0].upper()

        if not term:
            continue

        # Engine routing — determine which engine handles this event type
        engine = get_engine_for_event(event_type)
        if not engine:
            summary["skipped_no_corpus"] += 1
            add_to_watchlist(event_type, ticker_code, term,
                            market.get("yes_price", 0.5), market.get("volume", 0))
            continue

        if ticker_code in ingest_failed_this_cycle:
            summary["skipped_no_corpus"] += 1
            continue

        # 4a. Check corpus — auto-ingest if missing
        if not _has_corpus(event_type):
            log.info(f"No corpus for {ticker_code} — triggering auto-ingest...")
            try:
                from corpus.auto_ingest import auto_ingest
                ingest_result = auto_ingest(ticker_code)

                if ingest_result["status"] == "unknown_speaker":
                    add_to_watchlist(event_type, ticker_code, term,
                                    market.get("yes_price", 0.5), market.get("volume", 0))
                    ingest_failed_this_cycle.add(ticker_code)
                    summary["skipped_no_corpus"] += 1
                    continue

                if not ingest_result["ready_to_trade"]:
                    log.info(f"{ticker_code}: ingested {ingest_result['ingested']} "
                             f"but not enough (n={ingest_result['corpus_n']})")
                    ingest_failed_this_cycle.add(ticker_code)
                    summary["skipped_no_corpus"] += 1
                    continue

                log.info(f"{ticker_code}: auto-ingest complete (n={ingest_result['corpus_n']})")
                # Fall through to edge calculation

            except Exception as e:
                log.warning(f"Auto-ingest failed for {ticker_code}: {e}")
                ingest_failed_this_cycle.add(ticker_code)
                add_to_watchlist(event_type, ticker_code, term,
                                market.get("yes_price", 0.5), market.get("volume", 0))
                summary["skipped_no_corpus"] += 1
                continue

        # 4b. Price filter — skip already-settled or illiquid markets
        yes_price = market.get("yes_price", 0.5)
        kalshi_ticker = market.get("ticker", "")

        from config.settings import MIN_MARKET_PRICE, MAX_MARKET_PRICE, MIN_MARKET_VOLUME
        if yes_price < MIN_MARKET_PRICE or yes_price > MAX_MARKET_PRICE:
            continue  # already settled or no edge available

        # UPGRADE 7: Liquidity filter
        volume = market.get("volume", 0)
        if volume < MIN_MARKET_VOLUME:
            continue  # too thin to trade

        # UPGRADE 11: Time decay — compute hours to resolution
        hours_to_res = None
        try:
            event_dt = datetime.strptime(classified.get("event_date", ""), "%Y-%m-%d")
            hours_to_res = max(0, (event_dt - now).total_seconds() / 3600)
        except Exception:
            pass

        # Fetch ticker-specific news (cached per ticker)
        combined_headlines = list(headlines)  # start with macro
        if ticker_code not in ticker_headlines_cache:
            try:
                from engine.news import get_earnings_context
                ticker_headlines_cache[ticker_code] = get_earnings_context(ticker_code)
            except Exception:
                ticker_headlines_cache[ticker_code] = []
        combined_headlines.extend(ticker_headlines_cache.get(ticker_code, []))

        try:
            edge_result = calculate_edge(
                term=term,
                event_type=event_type,
                event_date=classified.get("event_date", ""),
                kalshi_ticker=kalshi_ticker,
                yes_price=yes_price,
                news_headlines=combined_headlines,
            )
        except Exception as e:
            log.warning(f"Edge calc failed for {term}/{event_type}: {e}")
            continue

        if not edge_result or edge_result["signal"] == "SKIP":
            continue

        # 4c. We have an opportunity — queue it for ranking
        summary["opportunities"] += 1
        _log_opportunity(edge_result)
        opportunity_queue.append(edge_result)
        log.info(f"  OPPORTUNITY: {term} ({event_type}) edge={edge_result['edge']*100:+.1f}pp signal={edge_result['signal']}")

    # 5. Rank opportunities and enforce limits
    from config.settings import MAX_DAILY_TRADES, MAX_TRADES_PER_EVENT
    opportunity_queue.sort(key=lambda r: r.get("edge", 0), reverse=True)

    # Enforce per-event limit: max N trades on same event_type
    event_counts = {}
    top_opportunities = []
    for opp in opportunity_queue:
        # Derive event_type from ticker
        opp_classified = classify_market({"ticker": opp.get("ticker", ""), "title": "", "event_title": "", "subtitle": ""})
        evt = opp_classified.get("event_type", "other")
        event_counts[evt] = event_counts.get(evt, 0) + 1
        if event_counts[evt] <= MAX_TRADES_PER_EVENT and len(top_opportunities) < MAX_DAILY_TRADES:
            top_opportunities.append(opp)

    if opportunity_queue:
        log.info(f"Ranked {len(opportunity_queue)} opportunities. "
                 f"Executing top {len(top_opportunities)} "
                 f"(max {MAX_DAILY_TRADES}/day, {MAX_TRADES_PER_EVENT}/event).")
        skipped = len(opportunity_queue) - len(top_opportunities)
        if skipped > 0:
            log.info(f"Skipped {skipped} opportunities (below cutoff or event limit)")

    for edge_result in top_opportunities:
        try:
            trade = executor.execute_opportunity(edge_result)
            if not trade.get("skipped"):
                summary["trades_placed"] += 1
        except Exception as e:
            log.warning(f"Execution failed for {term}: {e}")

    log.info(f"=== MORNING SCAN COMPLETE: {summary['opportunities']} opportunities, "
             f"{summary['trades_placed']} trades, {summary['skipped_no_corpus']} skipped (no corpus) ===")

    # Save summary
    summary_path = os.path.join(LOG_DIR, "last_morning_scan.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def midday_check() -> None:
    """Lightweight noon check — just resolution checking, no edge calc."""
    log.info("--- Midday check ---")
    from loop.reviewer import check_resolutions

    resolved = check_resolutions()
    if resolved:
        log.info(f"Midday: {len(resolved)} trades resolved!")
        from loop.reviewer import score_strategies
        score_strategies()
    else:
        log.info("Midday: no new resolutions.")


def evening_review() -> dict:
    """
    Daily 8:00 PM review. Resolve trades, score, extract lessons.
    """
    log.info("=== EVENING REVIEW START ===")

    from loop.reviewer import check_resolutions, score_strategies, extract_lessons

    # 1. Check resolutions
    resolved = check_resolutions()

    # 2. Score
    score_strategies()

    # 3. Extract lessons
    if resolved:
        lessons = extract_lessons(resolved)
        if lessons:
            lessons_path = os.path.join(os.path.dirname(__file__), "..", "LESSONS.md")
            if os.path.exists(lessons_path):
                with open(lessons_path, "a") as f:
                    f.write("\n" + "\n".join(lessons) + "\n")
                log.info(f"Appended {len(lessons)} lessons to LESSONS.md")

    # 4. Portfolio summary
    conn = _get_conn()
    open_count = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='OPEN'").fetchone()[0]
    total_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    resolved_count = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome IN ('WIN','LOSS')").fetchone()[0]
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE outcome IN ('WIN','LOSS')").fetchone()[0]
    wins = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='WIN'").fetchone()[0]
    conn.close()

    win_rate = wins / resolved_count * 100 if resolved_count > 0 else 0

    summary = {
        "timestamp": datetime.utcnow().isoformat(),
        "resolved_today": len(resolved) if resolved else 0,
        "open_trades": open_count,
        "total_trades": total_count,
        "total_resolved": resolved_count,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
    }

    log.info(f"=== EVENING REVIEW: {open_count} open, {resolved_count} resolved, "
             f"PnL=${total_pnl:+.2f}, WR={win_rate:.0f}% ===")

    # 5. Check live gate
    from loop.monitor import live_gate_check
    live_gate_check()

    summary_path = os.path.join(LOG_DIR, "last_evening_review.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def update_heartbeat() -> None:
    """Hourly heartbeat — proves the process is alive."""
    heartbeat_path = os.path.join(LOG_DIR, "heartbeat")
    with open(heartbeat_path, "w") as f:
        f.write(datetime.utcnow().isoformat())


def run_daily_loop() -> None:
    """
    Schedule-based daily loop.
    6am: morning scan, 12pm: midday check, 8pm: evening review.
    Hourly heartbeat.
    """
    log.info("=" * 60)
    log.info("SOVEREIGN DAILY AUTONOMOUS SCANNER")
    log.info(f"Paper mode: {PAPER_MODE}")
    log.info("Schedule: 6am scan / 12pm check / 8pm review")
    log.info("=" * 60)

    schedule.every().day.at("06:00").do(morning_scan)
    schedule.every().day.at("12:00").do(midday_check)
    schedule.every().day.at("20:00").do(evening_review)
    schedule.every().hour.do(update_heartbeat)

    # Graceful shutdown
    running = [True]
    def handler(sig, frame):
        log.info("Shutting down daily scanner...")
        running[0] = False
    signal.signal(signal.SIGINT, handler)

    # Initial heartbeat
    update_heartbeat()
    log.info(f"Next morning scan: {schedule.next_run()}")

    while running[0]:
        schedule.run_pending()
        time.sleep(60)

    log.info("Daily scanner stopped.")


def test_run() -> None:
    """Validate scan pipeline without placing trades or calling Claude API."""
    print("TEST RUN — no trades, no Claude API calls")

    from engine.scanner import KalshiClient, classify_market

    client = KalshiClient()
    markets = client.get_mention_markets()
    print(f"  Kalshi API: {len(markets)} open mention markets found")

    now = datetime.utcnow()
    cutoff = now + timedelta(hours=72)
    qualifying = []
    for m in markets:
        classified = classify_market(m)
        edate = classified.get("event_date")
        if not edate:
            continue
        try:
            edt = datetime.strptime(edate, "%Y-%m-%d")
        except ValueError:
            continue
        if now - timedelta(hours=24) <= edt <= cutoff:
            qualifying.append((m, classified))

    print(f"  Resolving within 72h: {len(qualifying)}")

    for m, c in qualifying[:5]:
        has = _has_corpus(c["event_type"])
        status = "HAS CORPUS" if has else "NO CORPUS -> watchlist"
        print(f"    {c['event_type']:<25} | {(c.get('term') or '?'):<20} | {status}")

    if len(qualifying) > 5:
        print(f"    ... +{len(qualifying) - 5} more")

    print("  Test run complete — pipeline healthy")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sovereign Daily Scanner")
    parser.add_argument("--test-run", action="store_true",
                        help="Validate pipeline without trades or API calls")
    parser.add_argument("--morning", action="store_true", help="Run morning scan now")
    parser.add_argument("--midday", action="store_true", help="Run midday check now")
    parser.add_argument("--evening", action="store_true", help="Run evening review now")
    parser.add_argument("--daemon", action="store_true", help="Run scheduled daily loop")
    parser.add_argument("--watchlist", action="store_true", help="Print watchlist report")
    args = parser.parse_args()

    if args.test_run:
        test_run()
    elif args.morning:
        morning_scan()
    elif args.midday:
        midday_check()
    elif args.evening:
        evening_review()
    elif args.watchlist:
        from config.watchlist import watchlist_report
        watchlist_report()
    elif args.daemon:
        run_daily_loop()
    else:
        morning_scan()
