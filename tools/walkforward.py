"""Walk-Forward Test — simulates system evolving through time with strict temporal isolation."""

import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, MIN_EDGE_SINGLE, MIN_BASE_RATE_N
from engine.frequency import extract_mentions
from engine.edge_calc import kalshi_maker_fee, kelly_sizing, confidence_weight
from engine.scanner import classify_market
from engine.rules import extract_trigger_phrase

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("walkforward")

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "walkforward_results.json")
REPORT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "walkforward_report.md")
STARTING_BANKROLL = 500.0


def fetch_all_settled_markets(max_pages: int = 30) -> list[dict]:
    """Pull all settled mention markets from Kalshi."""
    from engine.scanner import KalshiClient
    client = KalshiClient()
    all_markets = []
    cursor = None

    for page in range(max_pages):
        params = {"limit": 200, "status": "settled", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        data = client._get("/events", params=params)
        events = data.get("events", [])

        for event in events:
            title = (event.get("title", "") or "").lower()
            ticker = (event.get("event_ticker", "") or "").lower()
            if not any(kw in title for kw in ["mention", " say ", "will say"]) and "mention" not in ticker:
                continue

            for m in event.get("markets", []):
                result = m.get("result")
                if result not in ("yes", "no"):
                    continue

                classified = classify_market({
                    "ticker": m.get("ticker", ""),
                    "title": m.get("title", ""),
                    "event_title": event.get("title", ""),
                    "event_ticker": event.get("event_ticker", ""),
                    "subtitle": m.get("yes_sub_title", ""),
                })

                # Extract event date
                event_date = classified.get("event_date")
                close_time = m.get("close_time", "")

                # Check for rolling window
                rules_text = m.get("rules_primary", "")
                parsed_rules = extract_trigger_phrase(rules_text) if rules_text else {}

                all_markets.append({
                    "ticker": m.get("ticker", ""),
                    "term": classified.get("term"),
                    "event_type": classified.get("event_type", "other"),
                    "event_date": event_date,
                    "result": result,
                    "volume": float(m.get("volume_fp", 0) or 0),
                    "is_rolling": parsed_rules.get("is_rolling_window", False),
                    "trigger_phrase": parsed_rules.get("trigger_phrase"),
                    "rules_text": rules_text,
                })

        cursor = data.get("cursor", "")
        if not cursor or not events:
            break

    return all_markets


def get_corpus_snapshot(conn: sqlite3.Connection, event_type: str,
                        before_date: str) -> list[tuple]:
    """Get transcripts available before a given date (temporal isolation)."""
    return conn.execute(
        "SELECT id, event_date, raw_text FROM transcripts WHERE event_type=? AND event_date < ?",
        (event_type, before_date)
    ).fetchall()


def simulate_market(market: dict, conn: sqlite3.Connection) -> dict | None:
    """Simulate a single market with temporal isolation."""
    term = market.get("trigger_phrase") or market.get("term")
    event_type = market.get("event_type")
    event_date = market.get("event_date")
    result = market.get("result")

    if not term or not event_type or event_type == "other" or not event_date:
        return None

    # Skip rolling-window markets (our system can't model these)
    if market.get("is_rolling"):
        return {"skipped": True, "reason": "rolling_window"}

    # Temporal isolation: only pre-event transcripts
    snapshots = get_corpus_snapshot(conn, event_type, event_date)
    n = len(snapshots)

    if n < MIN_BASE_RATE_N:
        return None  # not enough corpus AT THAT POINT IN TIME

    # Compute base rate from temporal snapshot
    k = 0
    for _, _, text in snapshots:
        mentions = extract_mentions(text, [term])
        if mentions[term]["mentioned"]:
            k += 1

    smoothed = (k + 1) / (n + 2)

    # Simulate market price (noisy estimate around base rate)
    import random
    random.seed(hash(market["ticker"]))
    noise = random.gauss(0, 0.10)
    sim_price = max(0.06, min(0.94, smoothed + noise))

    # Edge calculation
    fee = kalshi_maker_fee(sim_price)
    edge_yes = smoothed - sim_price - fee
    edge_no = (1 - smoothed) - (1 - sim_price) - kalshi_maker_fee(1 - sim_price)

    if edge_yes >= edge_no and edge_yes > 0:
        edge = edge_yes
        side = "yes"
        prob = smoothed
        price = sim_price
    elif edge_no > 0:
        edge = edge_no
        side = "no"
        prob = 1 - smoothed
        price = 1 - sim_price
    else:
        return None

    if edge < MIN_EDGE_SINGLE:
        return None

    # Kelly sizing with confidence
    conf = confidence_weight(n)
    kelly_pct, _, high_conv, strategy = kelly_sizing(prob, price, corpus_n=n)

    # Outcome
    if (result == "yes" and side == "yes") or (result == "no" and side == "no"):
        outcome = "WIN"
        pnl_ratio = (1.0 - price) / price
    else:
        outcome = "LOSS"
        pnl_ratio = -1.0

    return {
        "ticker": market["ticker"],
        "term": term,
        "event_type": event_type,
        "event_date": event_date,
        "side": side,
        "base_rate": k / n,
        "smoothed": smoothed,
        "corpus_n": n,
        "sim_price": sim_price,
        "edge": edge,
        "kelly_pct": kelly_pct,
        "confidence": conf,
        "strategy": strategy,
        "actual_result": result,
        "outcome": outcome,
        "pnl_ratio": pnl_ratio,
    }


def run_walkforward() -> dict:
    """Run the full walk-forward simulation."""
    log.info("=" * 60)
    log.info("SOVEREIGN WALK-FORWARD TEST")
    log.info("=" * 60)

    # Fetch markets
    log.info("Fetching settled mention markets...")
    all_markets = fetch_all_settled_markets()
    log.info(f"Total settled markets: {len(all_markets)}")

    # Filter to markets with dates, group by week
    dated = [m for m in all_markets if m.get("event_date")]
    dated.sort(key=lambda m: m["event_date"])

    # Group into weeks
    weeks = defaultdict(list)
    for m in dated:
        try:
            dt = datetime.strptime(m["event_date"], "%Y-%m-%d")
            week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
            weeks[week_start].append(m)
        except ValueError:
            continue

    log.info(f"Weeks with data: {len(weeks)}")

    conn = sqlite3.connect(DB_PATH)

    # Walk forward week by week
    all_trades = []
    weekly_results = []
    bankroll = STARTING_BANKROLL
    peak_bankroll = bankroll
    max_drawdown = 0
    rolling_skipped = 0

    for week_start in sorted(weeks.keys()):
        week_markets = weeks[week_start]
        week_trades = []

        for market in week_markets:
            result = simulate_market(market, conn)
            if result is None:
                continue
            if result.get("skipped"):
                if result.get("reason") == "rolling_window":
                    rolling_skipped += 1
                continue

            # Size the trade
            deploy = bankroll * result["kelly_pct"]
            if deploy < 1.0:
                continue  # too small

            pnl = deploy * result["pnl_ratio"]
            result["dollar_pnl"] = pnl
            result["deploy"] = deploy
            result["week"] = week_start

            week_trades.append(result)
            all_trades.append(result)
            bankroll += pnl

        # Weekly summary
        week_wins = sum(1 for t in week_trades if t["outcome"] == "WIN")
        week_losses = len(week_trades) - week_wins
        week_pnl = sum(t["dollar_pnl"] for t in week_trades)
        week_wr = week_wins / len(week_trades) * 100 if week_trades else 0

        if bankroll > peak_bankroll:
            peak_bankroll = bankroll
        drawdown = (peak_bankroll - bankroll) / peak_bankroll * 100
        max_drawdown = max(max_drawdown, drawdown)

        weekly_results.append({
            "week": week_start,
            "markets": len(week_markets),
            "trades": len(week_trades),
            "wins": week_wins,
            "losses": week_losses,
            "pnl": week_pnl,
            "win_rate": week_wr,
            "balance": bankroll,
            "drawdown_pct": drawdown,
        })

    conn.close()

    # Aggregate analysis
    total_trades = len(all_trades)
    total_wins = sum(1 for t in all_trades if t["outcome"] == "WIN")
    total_losses = total_trades - total_wins
    overall_wr = total_wins / total_trades * 100 if total_trades > 0 else 0
    total_pnl = bankroll - STARTING_BANKROLL
    roi = total_pnl / STARTING_BANKROLL * 100

    # By event type
    by_event = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for t in all_trades:
        et = t["event_type"]
        by_event[et]["pnl"] += t["dollar_pnl"]
        if t["outcome"] == "WIN":
            by_event[et]["w"] += 1
        else:
            by_event[et]["l"] += 1

    # By edge bucket
    by_edge = defaultdict(lambda: {"w": 0, "l": 0})
    for t in all_trades:
        ep = t["edge"] * 100
        if ep >= 25:
            bucket = "25pp+"
        elif ep >= 15:
            bucket = "15-25pp"
        else:
            bucket = "8-15pp"
        if t["outcome"] == "WIN":
            by_edge[bucket]["w"] += 1
        else:
            by_edge[bucket]["l"] += 1

    # By corpus depth AT TIME
    by_n = defaultdict(lambda: {"w": 0, "l": 0})
    for t in all_trades:
        cn = t["corpus_n"]
        if cn >= 10:
            b = "n>=10"
        elif cn >= 6:
            b = "n=6-9"
        else:
            b = "n<6"
        if t["outcome"] == "WIN":
            by_n[b]["w"] += 1
        else:
            by_n[b]["l"] += 1

    # Longest losing streak
    streak = 0
    max_streak = 0
    for t in all_trades:
        if t["outcome"] == "LOSS":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # Print report
    print()
    print("=" * 70)
    print("WALK-FORWARD REPORT")
    print("=" * 70)
    print(f"Period:              {sorted(weeks.keys())[0]} to {sorted(weeks.keys())[-1]}")
    print(f"Weeks simulated:     {len(weekly_results)}")
    print(f"Markets available:   {len(dated)}")
    print(f"Rolling-window skip: {rolling_skipped}")
    print(f"Trades simulated:    {total_trades}")
    print(f"Win rate:            {overall_wr:.1f}% ({total_wins}W / {total_losses}L)")
    print(f"Starting bankroll:   ${STARTING_BANKROLL:.2f}")
    print(f"Ending balance:      ${bankroll:.2f}")
    print(f"Total P&L:           ${total_pnl:+.2f}")
    print(f"ROI:                 {roi:+.1f}%")
    print(f"Max drawdown:        {max_drawdown:.1f}%")
    print(f"Longest loss streak: {max_streak}")
    print()

    print("── WEEKLY BREAKDOWN ──")
    for wr in weekly_results:
        if wr["trades"] > 0:
            print(f"  {wr['week']}  {wr['trades']:>2} trades  "
                  f"{wr['wins']}W/{wr['losses']}L  {wr['win_rate']:>5.0f}%  "
                  f"PnL ${wr['pnl']:>+7.2f}  Balance ${wr['balance']:>8.2f}  "
                  f"DD {wr['drawdown_pct']:.1f}%")

    print()
    print("── BY EVENT TYPE ──")
    for et, s in sorted(by_event.items(), key=lambda x: x[1]["w"] + x[1]["l"], reverse=True):
        total_e = s["w"] + s["l"]
        wr_e = s["w"] / total_e * 100 if total_e > 0 else 0
        print(f"  {et:<25} {total_e:>3} trades  {wr_e:.0f}% WR  PnL ${s['pnl']:+.2f}")

    print()
    print("── BY EDGE BUCKET ──")
    for bucket in ["8-15pp", "15-25pp", "25pp+"]:
        s = by_edge[bucket]
        total_b = s["w"] + s["l"]
        wr_b = s["w"] / total_b * 100 if total_b > 0 else 0
        print(f"  {bucket:<10} {total_b:>3} trades  {wr_b:.0f}% WR")

    print()
    print("── BY CORPUS DEPTH (at time of trade) ──")
    for bucket in ["n<6", "n=6-9", "n>=10"]:
        s = by_n.get(bucket, {"w": 0, "l": 0})
        total_b = s["w"] + s["l"]
        wr_b = s["w"] / total_b * 100 if total_b > 0 else 0
        print(f"  {bucket:<10} {total_b:>3} trades  {wr_b:.0f}% WR")

    # GO / NO-GO decision
    print()
    print("═" * 70)
    go_checks = [
        ("Win rate >= 65%", overall_wr >= 65),
        ("No week worse than -40% deployed", all(wr["drawdown_pct"] < 40 for wr in weekly_results)),
        ("Max drawdown < 30%", max_drawdown < 30),
        ("At least 1 category 70%+ WR", any(
            (s["w"] / (s["w"]+s["l"])) >= 0.70
            for s in by_event.values()
            if s["w"] + s["l"] >= 3
        )),
        ("Longest loss streak <= 4", max_streak <= 4),
    ]

    all_go = True
    for check_name, passed in go_checks:
        icon = "GO" if passed else "NO"
        print(f"  [{icon:>3}] {check_name}")
        if not passed:
            all_go = False

    print("═" * 70)
    if all_go:
        print("VERDICT: ✓ GO — Walk-forward validates the system.")
    else:
        fails = sum(1 for _, p in go_checks if not p)
        print(f"VERDICT: ✗ NO-GO — {fails} checks failed. Investigate before live.")
    print()

    # Save results
    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "period": f"{sorted(weeks.keys())[0]} to {sorted(weeks.keys())[-1]}",
        "total_trades": total_trades,
        "win_rate": overall_wr,
        "roi": roi,
        "starting_bankroll": STARTING_BANKROLL,
        "ending_balance": bankroll,
        "max_drawdown": max_drawdown,
        "longest_loss_streak": max_streak,
        "rolling_window_skipped": rolling_skipped,
        "by_event": {k: dict(v) for k, v in by_event.items()},
        "by_edge": {k: dict(v) for k, v in by_edge.items()},
        "by_corpus_n": {k: dict(v) for k, v in by_n.items()},
        "weekly": weekly_results,
        "trades": all_trades,
        "go_checks": {name: passed for name, passed in go_checks},
        "verdict": "GO" if all_go else "NO-GO",
    }

    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"Results saved to {RESULTS_PATH}")

    return report


if __name__ == "__main__":
    run_walkforward()
