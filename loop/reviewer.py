"""Reviewer — checks resolutions, scores trades, extracts lessons."""

import json
import sqlite3
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH


def _get_conn():
    return sqlite3.connect(DB_PATH)


def check_resolutions() -> list[dict]:
    """
    Fetches all OPEN trades from the trades table.
    For each, calls Kalshi API to check if the market has settled.
    Updates trades with outcome and PnL.
    Returns list of newly resolved trades.
    """
    conn = _get_conn()
    open_trades = conn.execute(
        """SELECT id, kalshi_market_id, term, side, price, contracts, total_cost, fee_paid, paper
           FROM trades WHERE outcome = 'OPEN'"""
    ).fetchall()

    if not open_trades:
        print("No open trades to review.")
        conn.close()
        return []

    # Try to get Kalshi client for live resolution checks
    try:
        from engine.scanner import KalshiClient
        client = KalshiClient()
    except Exception:
        client = None

    resolved = []
    for trade_id, ticker, term, side, price, contracts, cost, fee, paper in open_trades:
        result = None
        settled_price = None

        if client and client.authenticated:
            try:
                raw = client._get(f"/markets/{ticker}")
                m = raw.get("market", raw)
                market_result = m.get("result", "")
                status = m.get("status", "")

                # Method 1: Explicit result field
                if market_result and market_result in ("yes", "no"):
                    result = market_result

                # Method 2: Price-based detection (primary — works even when status='active')
                if result is None:
                    yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
                    yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
                    last_price = float(m.get("last_price_dollars", 0) or 0)

                    # Market settled YES: ask/bid both near $1
                    if (yes_bid >= 0.95) or (last_price >= 0.97 and yes_ask >= 0.95):
                        result = "yes"
                    # Market settled NO: ask/bid both near $0
                    elif (yes_ask <= 0.05 and yes_ask > 0) or (last_price <= 0.03 and yes_bid <= 0.02):
                        result = "no"
                    # Last price at extremes with no active book
                    elif last_price >= 0.97 and yes_ask == 0 and yes_bid == 0:
                        result = "yes"
                    elif last_price <= 0.03 and yes_ask == 0 and yes_bid == 0:
                        result = "no"

            except Exception as e:
                print(f"  Could not check {ticker}: {e}")

        # Also check market_snapshots for settlement signals
        if result is None:
            snap = conn.execute(
                """SELECT yes_price FROM market_snapshots
                   WHERE kalshi_market_id = ? ORDER BY snapshot_at DESC LIMIT 1""",
                (ticker,)
            ).fetchone()
            if snap:
                last_price = snap[0]
                if last_price >= 0.99:
                    result = "yes"
                elif last_price <= 0.01:
                    result = "no"

        if result is None:
            continue  # still open

        # Compute outcome and PnL
        trade_side = side.upper()
        if (result == "yes" and trade_side == "YES") or (result == "no" and trade_side == "NO"):
            outcome = "WIN"
            pnl = (1.0 - price) * contracts - fee  # win pays $1 - cost per contract
        else:
            outcome = "LOSS"
            pnl = -cost  # lose the total cost

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """UPDATE trades SET outcome=?, pnl=?, resolved_at=? WHERE id=?""",
            (outcome, pnl, now, trade_id)
        )
        conn.commit()

        trade_dict = {
            "trade_id": trade_id,
            "ticker": ticker,
            "term": term,
            "side": trade_side,
            "price": price,
            "contracts": contracts,
            "cost": cost,
            "result": result,
            "outcome": outcome,
            "pnl": pnl,
            "paper": paper,
        }
        resolved.append(trade_dict)
        mode = "PAPER" if paper else "LIVE"
        print(f"  [{mode}] #{trade_id} {term} {trade_side} @{price*100:.0f}c"
              f" → {result.upper()} → {outcome} (PnL: ${pnl:+.2f})")

    conn.close()
    return resolved


def manual_resolve(trade_id: int, result: str) -> dict:
    """Manually resolve a trade when API can't determine the result."""
    conn = _get_conn()
    trade = conn.execute(
        "SELECT id, term, side, price, contracts, total_cost, fee_paid, paper FROM trades WHERE id=?",
        (trade_id,)
    ).fetchone()

    if not trade:
        print(f"Trade #{trade_id} not found.")
        conn.close()
        return {}

    tid, term, side, price, contracts, cost, fee, paper = trade
    result = result.lower()

    if (result == "yes" and side == "YES") or (result == "no" and side == "NO"):
        outcome = "WIN"
        pnl = (1.0 - price) * contracts - fee
    else:
        outcome = "LOSS"
        pnl = -cost

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE trades SET outcome=?, pnl=?, resolved_at=? WHERE id=?",
                 (outcome, pnl, now, tid))
    conn.commit()
    conn.close()

    mode = "PAPER" if paper else "LIVE"
    print(f"  [{mode}] #{tid} {term} {side} @{price*100:.0f}c → {result.upper()} → {outcome} (PnL: ${pnl:+.2f})")
    return {"trade_id": tid, "term": term, "outcome": outcome, "pnl": pnl}


def score_strategies() -> None:
    """Aggregates all resolved trades by strategy type and prints scorecard."""
    conn = _get_conn()

    # All resolved trades
    trades = conn.execute(
        """SELECT term, side, price, contracts, total_cost, outcome, pnl, paper
           FROM trades WHERE outcome IN ('WIN', 'LOSS')"""
    ).fetchall()

    if not trades:
        print("No resolved trades to score.")
        conn.close()
        return

    total_trades = len(trades)
    wins = sum(1 for t in trades if t[5] == "WIN")
    losses = total_trades - wins
    total_pnl = sum(t[6] or 0 for t in trades)
    total_cost = sum(t[4] for t in trades)
    win_rate = wins / total_trades * 100 if total_trades > 0 else 0
    roi = total_pnl / total_cost * 100 if total_cost > 0 else 0

    paper_trades = [t for t in trades if t[7]]
    live_trades = [t for t in trades if not t[7]]

    print("\nSTRATEGY SCORECARD")
    print("─" * 60)
    print(f"{'Category':<20} {'Trades':>6} {'W/L':>8} {'Win%':>6} {'P&L':>10} {'ROI':>8}")
    print("─" * 60)

    if paper_trades:
        pw = sum(1 for t in paper_trades if t[5] == "WIN")
        pl = len(paper_trades) - pw
        ppnl = sum(t[6] or 0 for t in paper_trades)
        pcost = sum(t[4] for t in paper_trades)
        prate = pw / len(paper_trades) * 100
        proi = ppnl / pcost * 100 if pcost > 0 else 0
        print(f"{'Paper':.<20} {len(paper_trades):>6} {f'{pw}W/{pl}L':>8} {prate:>5.1f}% {f'${ppnl:+.2f}':>10} {proi:>7.1f}%")

    if live_trades:
        lw = sum(1 for t in live_trades if t[5] == "WIN")
        ll = len(live_trades) - lw
        lpnl = sum(t[6] or 0 for t in live_trades)
        lcost = sum(t[4] for t in live_trades)
        lrate = lw / len(live_trades) * 100
        lroi = lpnl / lcost * 100 if lcost > 0 else 0
        print(f"{'Live':.<20} {len(live_trades):>6} {f'{lw}W/{ll}L':>8} {lrate:>5.1f}% {f'${lpnl:+.2f}':>10} {lroi:>7.1f}%")

    print("─" * 60)
    print(f"{'TOTAL':.<20} {total_trades:>6} {f'{wins}W/{losses}L':>8} {win_rate:>5.1f}% {f'${total_pnl:+.2f}':>10} {roi:>7.1f}%")
    print("─" * 60)

    # Upsert strategy_scores
    conn.execute("DELETE FROM strategy_scores")
    conn.execute(
        """INSERT INTO strategy_scores (strategy, trades, wins, losses, total_pnl, avg_roi)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("frequency_context", total_trades, wins, losses, total_pnl, roi)
    )
    conn.commit()
    conn.close()


def extract_lessons(resolved_trades: list[dict]) -> list[str]:
    """
    For each resolved trade, generate a one-sentence lesson.
    Uses Claude API if available, otherwise generates from data.
    """
    if not resolved_trades:
        return []

    lessons = []
    try:
        from engine.context_scorer import _get_anthropic_client
        client = _get_anthropic_client()
    except Exception:
        client = None

    for trade in resolved_trades:
        term = trade.get("term", "unknown")
        outcome = trade.get("outcome", "UNKNOWN")
        pnl = trade.get("pnl", 0)
        side = trade.get("side", "YES")
        price = trade.get("price", 0.5)

        if client:
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=100,
                    system="You output one sentence. No more.",
                    messages=[{"role": "user", "content": (
                        f"A mention market trade just resolved.\n"
                        f"Term: {term}, Event: earnings call\n"
                        f"Side: {side}, Price: {price*100:.0f}c\n"
                        f"Outcome: {outcome} (PnL: ${pnl:+.2f})\n"
                        f"What single lesson should the system remember?"
                    )}]
                )
                lesson = response.content[0].text.strip()
            except Exception:
                lesson = _fallback_lesson(trade)
        else:
            lesson = _fallback_lesson(trade)

        lessons.append(f"[{datetime.now().strftime('%Y-%m-%d')}] {term.upper()} {outcome}: {lesson}")

    return lessons


def _fallback_lesson(trade: dict) -> str:
    """Generate a lesson without Claude API."""
    if trade["outcome"] == "WIN":
        return (f"'{trade['term']}' {trade['side']} @{trade['price']*100:.0f}c was correct. "
                f"Base rate held. Market was {abs(trade['pnl'])/trade['cost']*100:.0f}% mispriced.")
    else:
        return (f"'{trade['term']}' {trade['side']} @{trade['price']*100:.0f}c lost. "
                f"Review whether base rate or context adjustment was wrong.")


def run_full_review() -> None:
    """Complete review cycle: check resolutions, score, extract lessons."""
    print("=" * 60)
    print("SOVEREIGN TRADE REVIEWER")
    print("=" * 60)

    print("\n1. Checking resolutions...")
    resolved = check_resolutions()

    if not resolved:
        print("   No trades resolved yet.\n")

    print("\n2. Strategy scorecard...")
    score_strategies()

    if resolved:
        print("\n3. Extracting lessons...")
        lessons = extract_lessons(resolved)
        for lesson in lessons:
            print(f"   {lesson}")

        # Append to LESSONS.md
        lessons_path = os.path.join(os.path.dirname(__file__), "..", "LESSONS.md")
        if os.path.exists(lessons_path):
            with open(lessons_path, "r") as f:
                content = f.read()
            marker = "Awaiting resolution after NFLX Q1 2026 call. Review tonight."
            insert_point = content.find(marker)
            if insert_point > 0:
                insert_point += len(marker)
                new_content = content[:insert_point] + "\n\n" + "\n".join(lessons) + content[insert_point:]
            else:
                # Append after TRADE LESSONS header
                marker2 = "## TRADE LESSONS (append here after every resolved trade)"
                idx = content.find(marker2)
                if idx > 0:
                    idx = content.find("\n", idx) + 1
                    new_content = content[:idx] + "\n" + "\n".join(lessons) + "\n" + content[idx:]
                else:
                    new_content = content + "\n" + "\n".join(lessons) + "\n"
            with open(lessons_path, "w") as f:
                f.write(new_content)
            print(f"\n   Appended {len(lessons)} lessons to LESSONS.md")

    print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sovereign Trade Reviewer")
    parser.add_argument("--resolve", type=int, help="Manually resolve trade by ID")
    parser.add_argument("--result", type=str, choices=["yes", "no"], help="Resolution result")
    parser.add_argument("--review", action="store_true", help="Run full review cycle")
    args = parser.parse_args()

    if args.resolve and args.result:
        manual_resolve(args.resolve, args.result)
        score_strategies()
    elif args.review:
        run_full_review()
    else:
        run_full_review()
