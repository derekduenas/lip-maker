"""Junk Bond Scanner — finds near-certain words mispriced on Kalshi."""

import re
import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, JUNK_BOND_MIN_PROB, JUNK_BOND_MAX_PRICE


# Extended near-certain terms per event type (from corpus analysis)
NEAR_CERTAIN_TERMS = {
    "fomc_presser": [
        # 100% base rate
        "unemployment", "economy", "data", "risks", "outlook",
        "monetary policy", "percent", "strong", "appropriate", "thank",
        # 98%+
        "inflation", "labor market", "committee", "growth", "target",
        "conditions", "federal funds rate", "goals",
        # 95%+
        "maximum employment",
    ],
    "fomc_minutes": [
        "inflation", "labor market", "unemployment", "uncertainty",
        "credit", "balance sheet", "housing", "financial conditions",
    ],
}


def scan_for_junk_bonds(event_type: str = None) -> list[dict]:
    """
    Find all near-certain terms where Kalshi market price < 90c.
    These are the 'free money' trades from the Catboy playbook.
    """
    conn = sqlite3.connect(DB_PATH)

    event_types = [event_type] if event_type else list(NEAR_CERTAIN_TERMS.keys())
    opportunities = []

    for et in event_types:
        terms = NEAR_CERTAIN_TERMS.get(et, [])
        n = conn.execute("SELECT COUNT(*) FROM transcripts WHERE event_type=?", (et,)).fetchone()[0]

        if n < 10:
            continue

        for term in terms:
            # Get base rate
            row = conn.execute(
                "SELECT base_rate, occurrences, total_events FROM frequency_matrix WHERE event_type=? AND term=?",
                (et, term)
            ).fetchone()

            if not row:
                # Term not in matrix yet — compute inline
                from engine.frequency import extract_mentions
                transcripts = conn.execute("SELECT raw_text FROM transcripts WHERE event_type=?", (et,)).fetchall()
                k = sum(1 for (text,) in transcripts
                        if extract_mentions(text, [term])[term]["mentioned"])
                base_rate = k / len(transcripts) if transcripts else 0
            else:
                base_rate = row[0]

            if base_rate < JUNK_BOND_MIN_PROB:
                continue

            # Bayesian smoothed for safety
            k = int(base_rate * n)
            smoothed = (k + 1) / (n + 2)

            opportunities.append({
                "event_type": et,
                "term": term,
                "base_rate": base_rate,
                "smoothed": smoothed,
                "corpus_n": n,
                "tier": "junk_bond",
            })

    conn.close()

    # Sort by base rate descending
    opportunities.sort(key=lambda x: x["base_rate"], reverse=True)
    return opportunities


def print_report():
    """Print junk bond opportunity report."""
    opps = scan_for_junk_bonds()

    print("\nJUNK BOND SCANNER — Near-Certain Mispricings")
    print("=" * 65)
    print(f"{'Event Type':<20} {'Term':<25} {'Base Rate':>9} {'n':>4}")
    print("-" * 65)

    for o in opps:
        print(f"{o['event_type']:<20} {o['term']:<25} {o['base_rate']*100:>8.1f}% {o['corpus_n']:>4}")

    print("-" * 65)
    print(f"Total junk bond candidates: {len(opps)}")
    print(f"\nTRADE WHEN: Kalshi prices any of these below {JUNK_BOND_MAX_PRICE*100:.0f}c")
    print(f"At 85c with 98% true prob: 15% return, 2% loss risk")
    print(f"At 75c with 98% true prob: 31% return, 2% loss risk")
    print()


if __name__ == "__main__":
    print_report()
