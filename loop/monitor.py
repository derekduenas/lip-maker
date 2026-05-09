"""Monitor — autonomous event-driven loop. Scans before events, reviews after."""

import argparse
import signal
import sqlite3
import sys
import os
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, PAPER_MODE, MIN_EDGE_SINGLE, MIN_BASE_RATE_N


def _get_conn():
    return sqlite3.connect(DB_PATH)


def run_pre_event_scan(hours_before: int = 24) -> None:
    """
    Checks config/events.py for events with scan_at within `hours_before`.
    For each upcoming event, runs edge scan and executes paper trades.
    """
    from config.events import get_events_needing_scan
    from engine.scanner import KalshiClient, classify_market
    from engine.edge_calc import calculate_edge, kalshi_maker_fee, kelly_sizing, _log_opportunity
    from engine.executor import Executor

    events = get_events_needing_scan(within_hours=hours_before)

    if not events:
        print(f"  No events needing scan within {hours_before}h.")
        return

    client = KalshiClient()
    executor = Executor()

    for event in events:
        event_type = event["event_type"]
        event_date = event["event_date"]
        speaker = event.get("speaker", "")
        print(f"\n  SCANNING: {event_type} on {event_date} (speaker: {speaker})")

        # Fetch all mention markets
        markets = client.get_mention_markets()

        # Filter to this event type
        matching = []
        for m in markets:
            classified = classify_market(m)
            if classified["event_type"] == event_type:
                matching.append((m, classified))

        if not matching:
            print(f"  No Kalshi mention markets found for {event_type}.")
            # Try broader matching by date
            for m in markets:
                classified = classify_market(m)
                if classified.get("event_date") == event_date:
                    matching.append((m, classified))
            if matching:
                print(f"  Found {len(matching)} markets by date match.")
            else:
                continue

        print(f"  Found {len(matching)} mention markets for {event_type}")

        # Current headlines for context
        headlines = [
            "Tariff uncertainty continues to weigh on markets",
            "Tech earnings season kicks off this week",
            "Fed holds steady, signals patience on rate changes",
            "Consumer spending remains resilient despite trade concerns",
        ]

        qualifying = 0
        for market, classified in matching:
            term = classified.get("term")
            if not term:
                continue

            yes_price = market.get("yes_price", 0.5)
            ticker = market.get("ticker", "")

            edge_result = calculate_edge(
                term=term,
                event_type=event_type,
                event_date=event_date,
                kalshi_ticker=ticker,
                yes_price=yes_price,
                news_headlines=headlines,
            )

            if edge_result and edge_result["signal"] != "SKIP":
                qualifying += 1
                _log_opportunity(edge_result)

                # Execute paper trade
                trade = executor.execute_opportunity(edge_result)
                if not trade.get("skipped"):
                    print(f"    {term}: edge={edge_result['edge']*100:+.1f}pp signal={edge_result['signal']}")

        print(f"  Scan complete: {qualifying} qualifying opportunities")


def run_post_event_review() -> None:
    """
    Checks for events that occurred in the last 48 hours.
    Runs reviewer to resolve trades and score strategies.
    """
    from config.events import get_recently_resolved
    from loop.reviewer import check_resolutions, score_strategies, extract_lessons

    events = get_recently_resolved(within_hours=48)
    if events:
        print(f"\n  Reviewing {len(events)} recently resolved events...")

    print("\n  Checking open trade resolutions...")
    resolved = check_resolutions()

    if resolved:
        print(f"\n  {len(resolved)} trades resolved. Scoring...")
        score_strategies()

        lessons = extract_lessons(resolved)
        if lessons:
            lessons_path = os.path.join(os.path.dirname(__file__), "..", "LESSONS.md")
            if os.path.exists(lessons_path):
                with open(lessons_path, "a") as f:
                    f.write("\n" + "\n".join(lessons) + "\n")
                print(f"  Appended {len(lessons)} lessons to LESSONS.md")
    else:
        print("  No trades resolved this cycle.")


def main_loop(interval_minutes: int = 60) -> None:
    """
    Runs every `interval_minutes`:
    1. Pre-event scan (24h lookahead)
    2. Post-event review (48h lookback)
    3. Print summary
    """
    print("=" * 60)
    print("SOVEREIGN MONITOR LOOP")
    print(f"Interval: {interval_minutes}min | Paper: {PAPER_MODE}")
    print("=" * 60)

    # Handle graceful shutdown
    running = [True]
    def handler(sig, frame):
        print("\n\nShutting down monitor loop...")
        running[0] = False
    signal.signal(signal.SIGINT, handler)

    cycle = 0
    while running[0]:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"\n--- Cycle {cycle} | {now} ---")

        try:
            print("\n[1/2] Pre-event scan (24h lookahead)...")
            run_pre_event_scan(hours_before=24)

            print("\n[2/2] Post-event review (48h lookback)...")
            run_post_event_review()

        except Exception as e:
            print(f"\n  ERROR in cycle {cycle}: {e}")

        # Summary
        conn = _get_conn()
        open_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='OPEN'").fetchone()[0]
        total_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        total_pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE outcome IN ('WIN','LOSS')").fetchone()[0]
        conn.close()

        print(f"\n  Summary: {open_trades} open / {total_trades} total trades | PnL: ${total_pnl:+.2f}")

        # Show next events
        from config.events import get_events_needing_scan, get_upcoming_events
        next_scans = get_events_needing_scan(within_hours=168)  # 7 days
        if next_scans:
            print(f"  Next scan targets:")
            for e in next_scans[:5]:
                print(f"    {e['event_type']} on {e['event_date']} (scan at {e['scan_at']})")

        if not running[0]:
            break

        print(f"\n  Sleeping {interval_minutes} minutes...")
        for _ in range(interval_minutes * 60):
            if not running[0]:
                break
            time.sleep(1)

    print("Monitor loop stopped.")


def live_gate_check() -> None:
    """Validates whether the system is ready for live trading."""
    conn = _get_conn()

    paper_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE paper=1").fetchone()[0]
    resolved_paper = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE paper=1 AND outcome IN ('WIN','LOSS')"
    ).fetchone()[0]
    paper_wins = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE paper=1 AND outcome='WIN'"
    ).fetchone()[0]
    paper_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE paper=1 AND outcome IN ('WIN','LOSS')"
    ).fetchone()[0]
    paper_cost = conn.execute(
        "SELECT COALESCE(SUM(total_cost), 0) FROM trades WHERE paper=1 AND outcome IN ('WIN','LOSS')"
    ).fetchone()[0]

    win_rate = paper_wins / resolved_paper * 100 if resolved_paper > 0 else 0
    roi = paper_pnl / paper_cost * 100 if paper_cost > 0 else 0

    # Avg edge vs actual outcome
    edge_rows = conn.execute(
        """SELECT t.pnl, t.price, t.contracts, o.edge
           FROM trades t
           LEFT JOIN opportunities o ON t.opportunity_id = o.id
           WHERE t.paper=1 AND t.outcome IN ('WIN','LOSS') AND o.edge IS NOT NULL"""
    ).fetchall()
    if edge_rows:
        avg_edge_pp = sum(r[3] for r in edge_rows) / len(edge_rows) * 100
    else:
        avg_edge_pp = 0.0

    # Count lessons
    lessons_path = os.path.join(os.path.dirname(__file__), "..", "LESSONS.md")
    lesson_count = 0
    if os.path.exists(lessons_path):
        with open(lessons_path) as f:
            for line in f:
                if line.strip().startswith("[2026-"):
                    lesson_count += 1

    strategies = conn.execute("SELECT COUNT(*) FROM strategy_scores").fetchone()[0]
    conn.close()

    print("\nSOVEREIGN LIVE TRADING GATE")
    print("=" * 60)

    checks = [
        ("Paper trades executed", resolved_paper, "20+", resolved_paper >= 20),
        ("Paper trade win rate", f"{win_rate:.1f}%", ">65%", win_rate > 65),
        ("Paper trade positive ROI", f"{roi:+.1f}%", ">0%", roi > 0),
        ("Avg edge vs actual outcome", f"{avg_edge_pp:+.1f}pp", ">5pp", avg_edge_pp > 5),
        ("Strategy scores computed", strategies, ">0", strategies > 0),
        ("LESSONS.md entries", lesson_count, ">5", lesson_count > 5),
        ("PAPER_MODE", str(PAPER_MODE), "False to go live", not PAPER_MODE),
    ]

    all_pass = True
    for name, value, required, passed in checks:
        icon = "OK" if passed else "FAIL"
        print(f"  [{icon:>4}] {name + ':':<30} {str(value):<12} (required: {required})")
        if not passed:
            all_pass = False

    print("=" * 60)
    if all_pass:
        print("STATUS: ALL CHECKS PASSED — READY TO GO LIVE")
    else:
        pending = sum(1 for _, _, _, p in checks if not p)
        print(f"STATUS: {pending} checks failing. Paper trade more before going live.")
        print(f"  Paper trades placed: {paper_trades} ({resolved_paper} resolved)")
        if resolved_paper < 20:
            print(f"  Need {20 - resolved_paper} more resolved paper trades.")
    print()
    return all_pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Monitor")
    parser.add_argument("--interval", type=int, default=None,
                        help="Legacy interval mode (minutes). Omit for daily schedule.")
    parser.add_argument("--live-gate-check", action="store_true", help="Run live trading gate check")
    parser.add_argument("--scan-now", action="store_true", help="Run one pre-event scan immediately")
    parser.add_argument("--review-now", action="store_true", help="Run post-event review immediately")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--morning", action="store_true", help="Run daily morning scan now")
    parser.add_argument("--evening", action="store_true", help="Run daily evening review now")
    parser.add_argument("--watchlist", action="store_true", help="Show no-corpus watchlist")
    args = parser.parse_args()

    if args.live_gate_check:
        passed = live_gate_check()
        sys.exit(0 if passed else 1)
    elif args.scan_now:
        run_pre_event_scan(hours_before=168)
    elif args.review_now:
        run_post_event_review()
    elif args.once:
        print("Running single monitor cycle...")
        run_pre_event_scan(hours_before=24)
        run_post_event_review()
    elif args.morning:
        from loop.daily_scan import morning_scan
        morning_scan()
    elif args.evening:
        from loop.daily_scan import evening_review
        evening_review()
    elif args.watchlist:
        from config.watchlist import watchlist_report
        watchlist_report()
    elif args.interval is not None:
        # Legacy interval-based mode
        main_loop(interval_minutes=args.interval)
    else:
        # Default: daily schedule-based mode
        from loop.daily_scan import run_daily_loop
        run_daily_loop()
