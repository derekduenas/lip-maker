"""Speaker Vocabulary Engine — speaker-specific frequency matrices and trend analysis."""

import argparse
import re
import sqlite3
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, MIN_BASE_RATE_N
from engine.frequency import extract_mentions, build_cooccurrence_matrix

SPEAKER_TERM_LISTS = {
    "musk": [
        "autonomy", "autonomous", "FSD", "full self-driving", "dojo",
        "optimus", "humanoid", "robot", "robotaxi", "cybertruck",
        "supercharger", "energy storage", "megapack",
        "demand", "production", "delivery", "margin", "profitability",
        "free cash flow", "capex",
        "mind-blowing", "insane", "revolutionary", "exponential",
        "first principles", "rate of innovation",
        "tariffs", "trade", "uncertainty", "China",
    ],
    "huang": [
        "blackwell", "hopper", "GPU", "data center", "inference",
        "training", "accelerated computing", "CUDA", "sovereign AI",
        "digital twin", "omniverse", "robotics",
        "incredible", "extraordinary",
        "revenue", "gross margin", "supply", "capacity",
        "export controls", "China", "tariffs",
    ],
    "cook": [
        "iPhone", "services", "wearables", "Mac", "iPad",
        "vision pro", "apple intelligence", "AI",
        "ecosystem", "installed base",
        "buyback", "dividend", "gross margin", "services revenue",
        "supply chain", "tariffs", "India", "manufacturing",
    ],
    "zuckerberg": [
        "llama", "meta AI", "ray-ban", "quest", "threads",
        "reels", "advantage plus", "WhatsApp", "monetization",
        "open source",
        "data center", "capex", "infrastructure", "GPUs",
        "regulatory", "EU", "antitrust",
    ],
    "huffman": [
        "daily active users", "DAU", "API", "data licensing",
        "advertising", "search", "Google",
        "community", "authentic", "conversation",
        "revenue growth", "ARPU", "international",
    ],
    "niccol": [
        "throughput", "digital", "mobile order", "new restaurant",
        "menu innovation",
        "inflation", "food costs", "labor costs", "tariffs", "avocado",
        "pricing power",
    ],
}

# Minimum corpus for earnings (lower than FOMC's 5 — small sample reality)
MIN_EARNINGS_N = 3


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _get_event_type(ticker: str) -> str:
    return f"{ticker.lower()}_earnings"


def build_speaker_frequency_matrix(speaker: str, ticker: str) -> dict:
    """
    Build frequency matrix for a specific speaker/ticker combo.
    Returns dict of {term: {rate, k, n, last_seen, quarters}}.
    """
    conn = _get_conn()
    event_type = _get_event_type(ticker)
    terms = SPEAKER_TERM_LISTS.get(speaker, [])

    rows = conn.execute(
        "SELECT id, event_date, raw_text FROM transcripts WHERE event_type=? ORDER BY event_date",
        (event_type,)
    ).fetchall()
    n = len(rows)

    if n == 0:
        conn.close()
        return {}

    results = {}
    for term in terms:
        k = 0
        last_seen = None
        quarter_data = []

        for tid, edate, text in rows:
            mention = extract_mentions(text, [term])
            mentioned = mention[term]["mentioned"]
            count = mention[term]["count"]
            if mentioned:
                k += 1
                last_seen = edate
            quarter_data.append({"date": edate, "mentioned": mentioned, "count": count})

        base_rate = k / n if n > 0 else 0.0
        smoothed = (k + 1) / (n + 2)

        # Upsert into frequency_matrix
        conn.execute(
            """INSERT INTO frequency_matrix (speaker, event_type, term, occurrences, total_events, base_rate)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(speaker, event_type, term) DO UPDATE SET
                 occurrences=excluded.occurrences, total_events=excluded.total_events,
                 base_rate=excluded.base_rate, last_updated=CURRENT_TIMESTAMP""",
            (speaker, event_type, term, k, n, base_rate)
        )

        results[term] = {
            "base_rate": base_rate,
            "smoothed": smoothed,
            "k": k, "n": n,
            "last_seen": last_seen,
            "quarters": quarter_data,
        }

    conn.commit()

    # Build co-occurrence
    # Populate mentions table first
    for tid, edate, text in rows:
        mentions = extract_mentions(text, terms)
        for term, data in mentions.items():
            snippets = " ||| ".join(data["context_snippets"]) if data["context_snippets"] else None
            conn.execute(
                """INSERT OR REPLACE INTO mentions (transcript_id, term, mentioned, mention_count, context_snippet)
                   VALUES (?, ?, ?, ?, ?)""",
                (tid, term, data["mentioned"], data["count"], snippets)
            )
    conn.commit()
    build_cooccurrence_matrix(event_type, terms)

    conn.close()
    print(f"Speaker matrix built: {speaker}/{ticker} — {n} transcripts, {len(terms)} terms")
    return results


def get_speaker_base_rate(speaker: str, term: str, ticker: str = None) -> dict | None:
    """
    Returns speaker-specific base rate with trend analysis.
    Returns None if n < MIN_EARNINGS_N.
    """
    conn = _get_conn()
    event_type = _get_event_type(ticker) if ticker else None

    if event_type:
        row = conn.execute(
            "SELECT base_rate, occurrences, total_events FROM frequency_matrix WHERE speaker=? AND event_type=? AND term=?",
            (speaker, event_type, term)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT base_rate, occurrences, total_events FROM frequency_matrix WHERE speaker=? AND term=?",
            (speaker, term)
        ).fetchone()

    if row is None:
        conn.close()
        return None

    base_rate, k, n = row
    if n < MIN_EARNINGS_N:
        conn.close()
        return None

    # Compute trend
    trend = compute_term_trend(speaker, term, ticker)

    # Last seen
    if event_type:
        last = conn.execute(
            """SELECT MAX(t.event_date) FROM transcripts t
               JOIN mentions m ON m.transcript_id = t.id
               WHERE t.event_type=? AND m.term=? AND m.mentioned=1""",
            (event_type, term)
        ).fetchone()
        last_seen = last[0] if last else None
    else:
        last_seen = None

    conn.close()
    return {
        "base_rate": base_rate,
        "smoothed": (k + 1) / (n + 2),
        "n": n, "k": k,
        "trend": trend,
        "last_seen": last_seen,
    }


def compute_term_trend(speaker: str, term: str, ticker: str = None) -> str:
    """
    Compares mention rate in last 2 quarters vs prior quarters.
    increasing: last 2 avg > prior avg by >15pp
    decreasing: last 2 avg < prior avg by >15pp
    stable: within 15pp
    """
    conn = _get_conn()
    event_type = _get_event_type(ticker) if ticker else None

    if not event_type:
        conn.close()
        return "stable"

    rows = conn.execute(
        """SELECT t.event_date, m.mentioned FROM transcripts t
           JOIN mentions m ON m.transcript_id = t.id
           WHERE t.event_type=? AND m.term=?
           ORDER BY t.event_date""",
        (event_type, term)
    ).fetchall()
    conn.close()

    if len(rows) < 4:
        return "stable"

    recent = rows[-2:]
    prior = rows[:-2]

    recent_rate = sum(1 for _, m in recent if m) / len(recent)
    prior_rate = sum(1 for _, m in prior if m) / len(prior)

    diff = recent_rate - prior_rate
    if diff > 0.15:
        return "increasing"
    elif diff < -0.15:
        return "decreasing"
    return "stable"


def speaker_frequency_report(speaker: str, ticker: str) -> None:
    """Print formatted speaker frequency report."""
    results = build_speaker_frequency_matrix(speaker, ticker)
    if not results:
        print(f"No data for {speaker}/{ticker}")
        return

    n = next(iter(results.values()))["n"]
    event_type = _get_event_type(ticker)

    print(f"\n{ticker.upper()} / {speaker.upper()} — SPEAKER FREQUENCY MATRIX (n={n} quarters)")
    print("=" * 80)
    print(f"{'Term':<25} | {'Rate':>5} | {'Trend':<12} | {'Last Seen':<12} | {'k/n':>5}")
    print("-" * 80)

    sorted_results = sorted(results.items(), key=lambda x: x[1]["base_rate"], reverse=True)
    actionable = []
    skip_high = []
    skip_low = []

    for term, data in sorted_results:
        rate = data["base_rate"]
        trend = compute_term_trend(speaker, term, ticker)
        last = data["last_seen"] or "never"
        k, n_val = data["k"], data["n"]

        flag = ""
        if rate > 0.90:
            skip_high.append(term)
            flag = " <- skip"
        elif rate < 0.05 and k == 0:
            skip_low.append(term)
            flag = " <- skip"
        elif 0.10 <= rate <= 0.80:
            actionable.append(term)
            if trend == "increasing":
                flag = " <- HIGH EDGE"

        trend_display = trend.upper()
        print(f"{term:<25} | {rate*100:>4.0f}% | {trend_display:<12} | {last:<12} | {k}/{n_val}")

    print("-" * 80)

    # Top correlated pairs
    conn = _get_conn()
    pairs = conn.execute(
        """SELECT term_a, term_b, p_b_given_a FROM cooccurrence
           WHERE event_type=? ORDER BY p_b_given_a DESC LIMIT 10""",
        (event_type,)
    ).fetchall()
    conn.close()

    if pairs:
        print(f"\nTOP CORRELATED PAIRS (parlay signals):")
        for ta, tb, p in pairs[:5]:
            ra = results.get(ta, {}).get("base_rate", 0)
            rb = results.get(tb, {}).get("base_rate", 0)
            if ra > 0 and rb > 0:
                true_joint = ra * p
                indep = ra * rb
                edge = (true_joint - indep) * 100
                if abs(edge) > 1:
                    print(f"  {ta} + {tb:<20} P(both)={true_joint:.3f} | indep={indep:.3f} | {edge:+.1f}pp")

    print(f"\nACTIONABLE RANGE (10-80%): {', '.join(actionable)}")
    print(f"SKIP (>90%): {', '.join(skip_high)}")
    print(f"SKIP (<5%): {', '.join(skip_low)}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Speaker Engine")
    parser.add_argument("--speaker", type=str, help="Speaker name (e.g., musk)")
    parser.add_argument("--ticker", type=str, help="Ticker (e.g., TSLA)")
    args = parser.parse_args()

    if args.speaker and args.ticker:
        speaker_frequency_report(args.speaker, args.ticker)
    else:
        print("Usage: python3 engine/speaker.py --speaker musk --ticker TSLA")
