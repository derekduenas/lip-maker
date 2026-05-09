"""Frequency matrix engine — term extraction, base rates, co-occurrence."""

import re
import sqlite3
import sys
import os
import argparse
from itertools import combinations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, FOMC_TERMS, MIN_BASE_RATE_N


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _build_term_pattern(term: str) -> re.Pattern:
    """Build a regex pattern for whole-word, case-insensitive matching."""
    escaped = re.escape(term)
    return re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)


def _get_context_snippets(text: str, term: str, max_snippets: int = 3) -> list[str]:
    """Extract surrounding sentences where the term appears."""
    # Split text into sentences (approximate)
    sentences = re.split(r'(?<=[.!?])\s+', text)
    snippets = []
    pattern = _build_term_pattern(term)
    for sent in sentences:
        if pattern.search(sent):
            snippet = sent.strip()[:300]
            snippets.append(snippet)
            if len(snippets) >= max_snippets:
                break
    return snippets


def extract_mentions(transcript_text: str, terms: list[str]) -> dict[str, dict]:
    """
    Returns for each term:
    {
        "mentioned": bool,
        "count": int,
        "context_snippets": list[str]  # up to 3 surrounding sentences
    }
    Case-insensitive. Match whole words (regex word boundaries).
    """
    results = {}
    for term in terms:
        pattern = _build_term_pattern(term)
        matches = pattern.findall(transcript_text)
        count = len(matches)
        snippets = _get_context_snippets(transcript_text, term) if count > 0 else []
        results[term] = {
            "mentioned": count > 0,
            "count": count,
            "context_snippets": snippets,
        }
    return results


def build_mentions_table(event_type: str, terms: list[str]) -> None:
    """Populate the mentions table for all transcripts of event_type."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, raw_text FROM transcripts WHERE event_type=?",
        (event_type,)
    ).fetchall()

    for transcript_id, raw_text in rows:
        mentions = extract_mentions(raw_text, terms)
        for term, data in mentions.items():
            snippets_text = " ||| ".join(data["context_snippets"]) if data["context_snippets"] else None
            conn.execute(
                """INSERT OR REPLACE INTO mentions (transcript_id, term, mentioned, mention_count, context_snippet)
                   VALUES (?, ?, ?, ?, ?)""",
                (transcript_id, term, data["mentioned"], data["count"], snippets_text)
            )
    conn.commit()
    conn.close()
    print(f"Mentions table populated for {len(rows)} transcripts, {len(terms)} terms")


def build_frequency_matrix(event_type: str, terms: list[str]) -> None:
    """
    Reads all transcripts of event_type from DB.
    For each term, computes: occurrences, total_events, base_rate.
    Upserts into frequency_matrix table.
    """
    conn = _get_conn()

    # First populate mentions if not already done
    build_mentions_table(event_type, terms)

    rows = conn.execute(
        "SELECT id, speaker FROM transcripts WHERE event_type=?",
        (event_type,)
    ).fetchall()
    total_events = len(rows)

    for term in terms:
        # Count how many transcripts mention this term + intensity stats
        occurrences = conn.execute(
            """SELECT COUNT(*) FROM mentions m
               JOIN transcripts t ON m.transcript_id = t.id
               WHERE t.event_type=? AND m.term=? AND m.mentioned=1""",
            (event_type, term)
        ).fetchone()[0]

        # Intensity: avg and max mention count per transcript
        intensity = conn.execute(
            """SELECT AVG(m.mention_count), MAX(m.mention_count) FROM mentions m
               JOIN transcripts t ON m.transcript_id = t.id
               WHERE t.event_type=? AND m.term=? AND m.mentioned=1""",
            (event_type, term)
        ).fetchone()
        avg_count = intensity[0] or 0
        max_count = intensity[1] or 0

        base_rate = occurrences / total_events if total_events > 0 else 0.0

        # NOTE: speaker='' (empty string) instead of NULL. SQLite treats NULL
        # as distinct in UNIQUE constraints, so NULL speakers caused ON CONFLICT
        # to never fire — each rebuild appended a new row instead of upserting.
        # See MEMORY: project_sovereign_ops (dedupe fix 2026-04-19).
        conn.execute(
            """INSERT INTO frequency_matrix (speaker, event_type, term, occurrences, total_events, base_rate, avg_count_per_mention, max_count)
               VALUES ('', ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(speaker, event_type, term) DO UPDATE SET
                 occurrences=excluded.occurrences,
                 total_events=excluded.total_events,
                 base_rate=excluded.base_rate,
                 avg_count_per_mention=excluded.avg_count_per_mention,
                 max_count=excluded.max_count,
                 last_updated=CURRENT_TIMESTAMP""",
            (event_type, term, occurrences, total_events, base_rate, avg_count, max_count)
        )

    conn.commit()
    conn.close()
    print(f"Frequency matrix built: {total_events} events, {len(terms)} terms")


def get_base_rate(event_type: str, term: str, speaker: str = None) -> float | None:
    """
    Returns base rate from frequency_matrix.
    Returns None if n < MIN_BASE_RATE_N.

    Defensive: ORDER BY total_events DESC LIMIT 1 picks the freshest row if
    legacy duplicates ever exist (see NULL-speaker bug fixed 2026-04-19).
    """
    conn = _get_conn()
    if speaker:
        row = conn.execute(
            """SELECT base_rate, total_events FROM frequency_matrix
               WHERE event_type=? AND term=? AND speaker=?
               ORDER BY total_events DESC LIMIT 1""",
            (event_type, term, speaker)
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT base_rate, total_events FROM frequency_matrix
               WHERE event_type=? AND term=?
               ORDER BY total_events DESC LIMIT 1""",
            (event_type, term)
        ).fetchone()
    conn.close()

    if row is None:
        return None
    base_rate, total_events = row
    if total_events < MIN_BASE_RATE_N:
        return None
    return base_rate


def get_base_rate_recency_weighted(
    event_type: str,
    term: str,
    *,
    recent_n: int = 10,
    recent_weight: float = 2.0,
) -> float | None:
    """Recency-weighted base rate (Lead Alpha audit 2026-04-19).

    Computes base_rate giving `recent_weight`x weight to the most recent
    `recent_n` corpus events. Rationale: Trump's rhetoric shifts meaningfully
    over 6-12 months (e.g., "trillion" usage rose from ~50% in 2024 campaign
    to 90%+ in 2026). A flat average across the full corpus dilutes current
    signal with stale data.

    Not wired into the default path because effects are ASYMMETRIC:
      - Helps on trend-up terms ("trillion" 76%→79% under 2x, →90% recent-only)
      - Helps on trend-down terms ("drill baby drill" 10%→8%, signals disuse)
      - Can WORSEN on trending-up YES bets that happen to miss on a specific
        occurrence (e.g., "barack hussein obama" went 5%→70% recently; a
        specific speech not containing it becomes a larger loss at higher Kelly)

    Callers should use this deliberately for known trend-sensitive speakers.
    Defaults to same threshold (n >= MIN_BASE_RATE_N) as standard get_base_rate.

    Returns None if insufficient data.
    """
    conn = _get_conn()
    trans = conn.execute(
        "SELECT id FROM transcripts WHERE event_type=? ORDER BY event_date DESC",
        (event_type,),
    ).fetchall()
    if len(trans) < MIN_BASE_RATE_N:
        conn.close()
        return None

    recent_ids = {r[0] for r in trans[:recent_n]}
    all_ids = {r[0] for r in trans}
    older_ids = all_ids - recent_ids

    def _count_mentions(ids):
        if not ids:
            return 0
        placeholders = ",".join(["?"] * len(ids))
        return conn.execute(
            f"SELECT COUNT(*) FROM mentions WHERE term=? AND mentioned=1 "
            f"AND transcript_id IN ({placeholders})",
            (term, *ids),
        ).fetchone()[0]

    recent_hits = _count_mentions(recent_ids)
    older_hits = _count_mentions(older_ids)
    conn.close()

    weighted_hits = recent_weight * recent_hits + older_hits
    weighted_total = recent_weight * len(recent_ids) + len(older_ids)
    if weighted_total <= 0:
        return None
    return weighted_hits / weighted_total


# --- TASK 1.4: Co-occurrence ---

def build_cooccurrence_matrix(event_type: str, terms: list[str]) -> None:
    """
    For every pair (term_a, term_b):
    - Count events where both appeared
    - Count events where term_a appeared
    - Compute P(term_b | term_a)
    Upsert into cooccurrence table.
    """
    conn = _get_conn()

    # Get all transcript IDs for this event type
    transcript_ids = [r[0] for r in conn.execute(
        "SELECT id FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchall()]

    # Build a lookup: term -> set of transcript_ids where mentioned
    term_transcripts = {}
    for term in terms:
        ids = set()
        for row in conn.execute(
            """SELECT m.transcript_id FROM mentions m
               JOIN transcripts t ON m.transcript_id = t.id
               WHERE t.event_type=? AND m.term=? AND m.mentioned=1""",
            (event_type, term)
        ).fetchall():
            ids.add(row[0])
        term_transcripts[term] = ids

    # Compute co-occurrence for all pairs
    for term_a, term_b in combinations(terms, 2):
        a_set = term_transcripts.get(term_a, set())
        b_set = term_transcripts.get(term_b, set())
        both = len(a_set & b_set)
        a_count = len(a_set)
        b_count = len(b_set)

        # P(B|A)
        p_b_given_a = both / a_count if a_count > 0 else 0.0
        conn.execute(
            """INSERT INTO cooccurrence (event_type, term_a, term_b, both_mentioned, a_mentioned, p_b_given_a)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_type, term_a, term_b) DO UPDATE SET
                 both_mentioned=excluded.both_mentioned,
                 a_mentioned=excluded.a_mentioned,
                 p_b_given_a=excluded.p_b_given_a""",
            (event_type, term_a, term_b, both, a_count, p_b_given_a)
        )

        # P(A|B)
        p_a_given_b = both / b_count if b_count > 0 else 0.0
        conn.execute(
            """INSERT INTO cooccurrence (event_type, term_a, term_b, both_mentioned, a_mentioned, p_b_given_a)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_type, term_a, term_b) DO UPDATE SET
                 both_mentioned=excluded.both_mentioned,
                 a_mentioned=excluded.a_mentioned,
                 p_b_given_a=excluded.p_b_given_a""",
            (event_type, term_b, term_a, both, b_count, p_a_given_b)
        )

    conn.commit()
    conn.close()
    n_pairs = len(terms) * (len(terms) - 1)
    print(f"Co-occurrence matrix built: {n_pairs} directional pairs")


def get_conditional_prob(event_type: str, term_a: str, term_b: str) -> float:
    """
    Returns P(term_b | term_a) from co-occurrence table.
    Falls back to P(term_b) base rate if pair not found.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT p_b_given_a FROM cooccurrence WHERE event_type=? AND term_a=? AND term_b=?",
        (event_type, term_a, term_b)
    ).fetchone()
    conn.close()

    if row is not None:
        return row[0]

    # Fallback to base rate
    base = get_base_rate(event_type, term_b)
    return base if base is not None else 0.0


def detect_parlay_edge(
    event_type: str,
    term_a: str, market_price_a: float,
    term_b: str, market_price_b: float,
    parlay_market_price: float
) -> dict:
    """
    Returns parlay edge analysis.
    true_joint  = P(a) * P(b|a) using co-occurrence
    market_joint = market_price_a * market_price_b (independence assumption)
    parlay_edge  = true_joint - parlay_market_price - fees
    """
    from config.settings import MIN_EDGE_PARLAY

    p_a = get_base_rate(event_type, term_a) or 0.0
    p_b_given_a = get_conditional_prob(event_type, term_a, term_b)

    true_joint = p_a * p_b_given_a
    market_joint = market_price_a * market_price_b

    # Approximate parlay fee
    fee = 0.004375 * abs(2 * parlay_market_price - 1)
    parlay_edge = true_joint - parlay_market_price - fee

    return {
        "term_a": term_a,
        "term_b": term_b,
        "p_a": p_a,
        "p_b_given_a": p_b_given_a,
        "true_joint_prob": true_joint,
        "market_joint_prob": market_joint,
        "parlay_market_price": parlay_market_price,
        "fee": fee,
        "parlay_edge": parlay_edge,
        "trade_signal": parlay_edge > MIN_EDGE_PARLAY,
    }


# --- TASK 1.5: CLI Report ---

def print_frequency_report(event_type: str) -> None:
    """Print formatted frequency matrix report."""
    conn = _get_conn()

    total = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchone()[0]

    rows = conn.execute(
        """SELECT fm.term, fm.occurrences, fm.base_rate,
                  (SELECT MAX(t.event_date) FROM transcripts t
                   JOIN mentions m ON m.transcript_id = t.id
                   WHERE t.event_type=? AND m.term=fm.term AND m.mentioned=1) as last_seen
           FROM frequency_matrix fm
           WHERE fm.event_type=?
           ORDER BY fm.base_rate DESC""",
        (event_type, event_type)
    ).fetchall()

    label = event_type.upper().replace("_", " ")
    print(f"\n{label} — FREQUENCY MATRIX (n={total} events)")
    print("─" * 65)
    print(f"{'Term':<25} | {'Occurrences':>11} | {'Rate':>6} | {'Last Seen':<10}")
    print("─" * 65)
    for term, occ, rate, last_seen in rows:
        last = last_seen or "N/A"
        print(f"{term:<25} | {occ:>11} | {rate*100:>5.1f}% | {last:<10}")
    print("─" * 65)

    # Top correlated pairs
    print("\nTOP CORRELATED PAIRS (parlay signals):")
    pairs = conn.execute(
        """SELECT c.term_a, c.term_b, c.p_b_given_a,
                  fa.base_rate as rate_a, fb.base_rate as rate_b,
                  c.both_mentioned, c.a_mentioned
           FROM cooccurrence c
           JOIN frequency_matrix fa ON fa.event_type=c.event_type AND fa.term=c.term_a
           JOIN frequency_matrix fb ON fb.event_type=c.event_type AND fb.term=c.term_b
           WHERE c.event_type=?
             AND fa.base_rate > 0.2 AND fb.base_rate > 0.2
             AND fa.base_rate < 0.95 AND fb.base_rate < 0.95
           ORDER BY (fa.base_rate * c.p_b_given_a) - (fa.base_rate * fb.base_rate) DESC
           LIMIT 10""",
        (event_type,)
    ).fetchall()

    for term_a, term_b, p_b_given_a, rate_a, rate_b, both, a_count in pairs:
        true_joint = rate_a * p_b_given_a
        indep_joint = rate_a * rate_b
        edge_pp = (true_joint - indep_joint) * 100
        signal = f"edge +{edge_pp:.1f}pp" if edge_pp > 0 else f"edge {edge_pp:.1f}pp (skip)"
        print(f"  {term_a} + {term_b:<20} → P(both) = {true_joint:.3f} | mkt assumes {indep_joint:.3f} | {signal}")

    conn.close()
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Frequency Engine")
    parser.add_argument("--report", type=str, help="Print frequency report for event_type")
    parser.add_argument("--build", type=str, help="Build frequency matrix for event_type")
    args = parser.parse_args()

    if args.build:
        print(f"Building frequency matrix for {args.build}...")
        build_frequency_matrix(args.build, FOMC_TERMS)
        build_cooccurrence_matrix(args.build, FOMC_TERMS)
        print("Done.")

    if args.report:
        print_frequency_report(args.report)

    if not args.build and not args.report:
        # Default: build + report
        print("Building frequency matrix for fomc_presser...")
        build_frequency_matrix("fomc_presser", FOMC_TERMS)
        build_cooccurrence_matrix("fomc_presser", FOMC_TERMS)
        print_frequency_report("fomc_presser")
