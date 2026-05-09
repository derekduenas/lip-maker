"""Earnings Parlay Optimizer — co-occurrence analysis for earnings term pairs."""

import re
import sqlite3
import sys
import os
from itertools import combinations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, MIN_EDGE_PARLAY
from engine.frequency import extract_mentions, get_conditional_prob
from engine.edge_calc import kalshi_maker_fee
from engine.speaker import SPEAKER_TERM_LISTS


def _get_conn():
    return sqlite3.connect(DB_PATH)


def build_earnings_parlay_matrix(speaker: str, ticker: str) -> None:
    """Build co-occurrence matrix for speaker's term pairs."""
    conn = _get_conn()
    event_type = f"{ticker.lower()}_earnings"
    terms = SPEAKER_TERM_LISTS.get(speaker, [])

    rows = conn.execute(
        "SELECT id, raw_text FROM transcripts WHERE event_type=?",
        (event_type,)
    ).fetchall()

    if not rows or not terms:
        conn.close()
        return

    # Build lookup: term -> set of transcript IDs where mentioned
    term_tids = {}
    for term in terms:
        ids = set()
        for tid, text in rows:
            r = extract_mentions(text, [term])
            if r[term]["mentioned"]:
                ids.add(tid)
        term_tids[term] = ids

    # Compute co-occurrence for all pairs
    count = 0
    for ta, tb in combinations(terms, 2):
        a_set = term_tids.get(ta, set())
        b_set = term_tids.get(tb, set())
        both = len(a_set & b_set)
        a_count = len(a_set)
        b_count = len(b_set)

        if a_count > 0:
            p_b_given_a = both / a_count
            conn.execute(
                """INSERT INTO cooccurrence (event_type, term_a, term_b, both_mentioned, a_mentioned, p_b_given_a)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_type, term_a, term_b) DO UPDATE SET
                     both_mentioned=excluded.both_mentioned, a_mentioned=excluded.a_mentioned,
                     p_b_given_a=excluded.p_b_given_a""",
                (event_type, ta, tb, both, a_count, p_b_given_a)
            )
            count += 1

        if b_count > 0:
            p_a_given_b = both / b_count
            conn.execute(
                """INSERT INTO cooccurrence (event_type, term_a, term_b, both_mentioned, a_mentioned, p_b_given_a)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_type, term_a, term_b) DO UPDATE SET
                     both_mentioned=excluded.both_mentioned, a_mentioned=excluded.a_mentioned,
                     p_b_given_a=excluded.p_b_given_a""",
                (event_type, tb, ta, both, b_count, p_a_given_b)
            )
            count += 1

    conn.commit()
    conn.close()
    print(f"Earnings parlay matrix built: {speaker}/{ticker} — {count} directional pairs")


def scan_earnings_parlays(ticker: str, markets: list[dict] = None) -> list[dict]:
    """Find parlay opportunities for this ticker's earnings markets."""
    conn = _get_conn()
    event_type = f"{ticker.lower()}_earnings"

    # Get all co-occurrence pairs with edge potential
    pairs = conn.execute(
        """SELECT c.term_a, c.term_b, c.p_b_given_a, c.both_mentioned, c.a_mentioned,
                  fa.base_rate as rate_a, fb.base_rate as rate_b
           FROM cooccurrence c
           JOIN frequency_matrix fa ON fa.event_type=c.event_type AND fa.term=c.term_a
           JOIN frequency_matrix fb ON fb.event_type=c.event_type AND fb.term=c.term_b
           WHERE c.event_type=?
             AND fa.base_rate BETWEEN 0.10 AND 0.90
             AND fb.base_rate BETWEEN 0.10 AND 0.90""",
        (event_type,)
    ).fetchall()
    conn.close()

    results = []
    for ta, tb, p_b_given_a, both, a_count, rate_a, rate_b in pairs:
        true_joint = rate_a * p_b_given_a
        indep_joint = rate_a * rate_b
        edge = true_joint - indep_joint

        if edge > 0.03:  # at least 3pp edge to be interesting
            results.append({
                "term_a": ta, "term_b": tb,
                "true_joint": true_joint, "indep_joint": indep_joint,
                "edge_pp": edge * 100,
                "p_b_given_a": p_b_given_a,
                "rate_a": rate_a, "rate_b": rate_b,
            })

    results.sort(key=lambda x: x["edge_pp"], reverse=True)
    return results


def narrative_block_detector(speaker: str, ticker: str) -> list[list[str]]:
    """
    Detect narrative blocks — clusters of terms that always fire together.
    Returns lists of co-occurring terms.
    """
    conn = _get_conn()
    event_type = f"{ticker.lower()}_earnings"

    # Find term pairs with P(B|A) > 0.80 — very strong co-occurrence
    pairs = conn.execute(
        """SELECT term_a, term_b, p_b_given_a FROM cooccurrence
           WHERE event_type=? AND p_b_given_a > 0.80
           ORDER BY p_b_given_a DESC""",
        (event_type,)
    ).fetchall()
    conn.close()

    # Build clusters using union-find-like grouping
    clusters = {}
    for ta, tb, p in pairs:
        # Find existing cluster or create new one
        found = None
        for cid, members in clusters.items():
            if ta in members or tb in members:
                members.add(ta)
                members.add(tb)
                found = cid
                break
        if found is None:
            clusters[len(clusters)] = {ta, tb}

    # Convert to sorted lists
    blocks = [sorted(members) for members in clusters.values() if len(members) >= 2]
    blocks.sort(key=len, reverse=True)
    return blocks


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", nargs=2, metavar=("SPEAKER", "TICKER"))
    parser.add_argument("--scan", type=str, help="Scan parlays for ticker")
    parser.add_argument("--blocks", nargs=2, metavar=("SPEAKER", "TICKER"))
    args = parser.parse_args()

    if args.build:
        build_earnings_parlay_matrix(args.build[0], args.build[1])
    elif args.scan:
        results = scan_earnings_parlays(args.scan)
        if results:
            print(f"\nPARLAY SIGNALS — {args.scan.upper()}")
            for r in results[:10]:
                print(f"  {r['term_a']} + {r['term_b']:<20} true={r['true_joint']:.3f} indep={r['indep_joint']:.3f} edge={r['edge_pp']:+.1f}pp")
        else:
            print("No parlay signals found.")
    elif args.blocks:
        blocks = narrative_block_detector(args.blocks[0], args.blocks[1])
        print(f"\nNARRATIVE BLOCKS — {args.blocks[1].upper()}")
        for block in blocks:
            print(f"  [{', '.join(block)}]")
