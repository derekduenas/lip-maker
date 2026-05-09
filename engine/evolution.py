"""Term Evolution Tracker — detects emerging vocabulary and tracks term trends."""

import argparse
import re
import sqlite3
import sys
import os
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH

# Common stopwords and boilerplate to filter out
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "and", "but", "or", "nor",
    "not", "no", "so", "if", "then", "than", "that", "this", "these",
    "those", "it", "its", "he", "she", "they", "them", "we", "us", "you",
    "i", "my", "your", "our", "their", "his", "her", "of", "in", "to",
    "for", "with", "on", "at", "from", "by", "about", "as", "into",
    "through", "during", "before", "after", "above", "below", "between",
    "out", "off", "up", "down", "over", "under", "again", "further",
    "very", "just", "also", "more", "most", "other", "some", "such",
    "only", "own", "same", "too", "really", "think", "know", "going",
    "right", "well", "yeah", "yes", "okay", "thank", "thanks", "question",
    "actually", "like", "lot", "much", "many", "get", "got", "one", "two",
    "first", "last", "next", "new", "good", "great", "thing", "things",
    "back", "still", "even", "now", "here", "there", "when", "what",
    "how", "which", "who", "where", "why", "all", "each", "every",
    "both", "few", "little", "bit", "make", "take", "come", "go", "see",
    "look", "want", "give", "use", "way", "time", "year", "quarter",
    "call", "company", "business", "market", "percent", "million", "billion",
    "point", "part", "side", "kind", "term", "number", "said", "say",
    "around", "sort", "something", "anything", "everything",
}

BOILERPLATE_PHRASES = {
    "good afternoon", "good morning", "thank you", "thanks for",
    "operator", "please proceed", "forward-looking statements",
    "safe harbor", "securities litigation", "actual results may differ",
    "non-gaap", "earnings per share", "press release",
}


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _tokenize_bigrams(text: str) -> Counter:
    """Extract meaningful 1-grams and 2-grams from transcript text."""
    text = text.lower()
    words = re.findall(r'\b[a-z][a-z-]+\b', text)
    words = [w for w in words if w not in STOPWORDS and len(w) > 2]

    counts = Counter()
    # Unigrams
    for w in words:
        counts[w] += 1
    # Bigrams
    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i+1]}"
        if not any(bp in bigram for bp in BOILERPLATE_PHRASES):
            counts[bigram] += 1

    return counts


def build_evolution_chart(speaker: str, term: str, ticker: str) -> list[dict]:
    """Returns quarter-by-quarter mention data for a term."""
    conn = _get_conn()
    event_type = f"{ticker.lower()}_earnings"

    rows = conn.execute(
        "SELECT id, event_date, raw_text FROM transcripts WHERE event_type=? ORDER BY event_date",
        (event_type,)
    ).fetchall()
    conn.close()

    from engine.frequency import extract_mentions

    chart = []
    for tid, edate, text in rows:
        result = extract_mentions(text, [term])
        mentioned = result[term]["mentioned"]
        count = result[term]["count"]
        snippets = result[term]["context_snippets"]
        chart.append({
            "date": edate,
            "mentioned": mentioned,
            "count": count,
            "context": snippets[0] if snippets else None,
        })

    return chart


def detect_emerging_terms(speaker: str, ticker: str, recent_n: int = 2, prior_n: int = 6) -> list[dict]:
    """
    Find terms appearing in last `recent_n` quarters but NOT in prior `prior_n`.
    These are new vocabulary items the market hasn't priced.
    """
    conn = _get_conn()
    event_type = f"{ticker.lower()}_earnings"

    rows = conn.execute(
        "SELECT event_date, raw_text FROM transcripts WHERE event_type=? ORDER BY event_date",
        (event_type,)
    ).fetchall()
    conn.close()

    if len(rows) < recent_n + 1:
        return []

    # Split into recent and prior
    recent_texts = [text for _, text in rows[-recent_n:]]
    prior_texts = [text for _, text in rows[:-recent_n]]

    # Tokenize
    recent_counts = Counter()
    for text in recent_texts:
        recent_counts += _tokenize_bigrams(text)

    prior_counts = Counter()
    for text in prior_texts:
        prior_counts += _tokenize_bigrams(text)

    # Find NEW terms: >= 3 mentions in recent, 0 in prior
    emerging = []
    for term, count in recent_counts.most_common():
        if count >= 3 and prior_counts.get(term, 0) == 0:
            first_seen = rows[-recent_n][0]  # first of recent batch
            emerging.append({
                "term": term,
                "recent_count": count,
                "prior_count": 0,
                "first_seen": first_seen,
            })

    # Sort by frequency
    emerging.sort(key=lambda x: x["recent_count"], reverse=True)
    return emerging[:30]  # top 30


def emerging_terms_report(ticker: str) -> None:
    """Print emerging vocabulary report."""
    conn = _get_conn()
    event_type = f"{ticker.lower()}_earnings"

    # Get speaker
    row = conn.execute(
        "SELECT speaker FROM transcripts WHERE event_type=? AND speaker IS NOT NULL LIMIT 1",
        (event_type,)
    ).fetchone()
    speaker = row[0] if row else "unknown"
    conn.close()

    emerging = detect_emerging_terms(speaker, ticker)

    print(f"\nEMERGING VOCABULARY TRACKER — {ticker.upper()} / {speaker.upper()}")
    print("=" * 65)

    if not emerging:
        print("No emerging terms detected (need more quarters of data).")
        print()
        return

    print("Terms appearing in last 2 quarters NOT seen in prior quarters:")
    print("-" * 65)

    for e in emerging[:15]:
        print(f'  "{e["term"]}"'.ljust(30) + f'| {e["recent_count"]} mentions | First: {e["first_seen"]} | WATCH')

    print("-" * 65)
    print("ACTION: Add these to term list. If Kalshi lists them — you have data they don't.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Term Evolution Tracker")
    parser.add_argument("--emerging", type=str, help="Detect emerging terms for ticker")
    parser.add_argument("--chart", nargs=2, metavar=("TICKER", "TERM"), help="Show evolution chart")
    args = parser.parse_args()

    if args.emerging:
        emerging_terms_report(args.emerging)
    elif args.chart:
        ticker, term = args.chart
        chart = build_evolution_chart("", term, ticker)
        print(f"\nEvolution: '{term}' in {ticker.upper()}")
        for entry in chart:
            mark = "YES" if entry["mentioned"] else " - "
            print(f"  {entry['date']} | {mark} | count={entry['count']}")
    else:
        print("Usage: python3 engine/evolution.py --emerging TSLA")
