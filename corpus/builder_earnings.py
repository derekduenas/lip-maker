"""Earnings Corpus Builder — scrapes earnings call transcripts from Motley Fool."""

import re
import sqlite3
import time
import sys
import os

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}


def _get_conn():
    return sqlite3.connect(DB_PATH)


def already_ingested(conn, source, event_date, event_type):
    row = conn.execute(
        "SELECT id FROM transcripts WHERE source=? AND event_date=? AND event_type=?",
        (source, event_date, event_type)
    ).fetchone()
    return row is not None


def fetch_motley_fool_transcript(url: str) -> str:
    """Fetch and clean a Motley Fool transcript page."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Motley Fool article body
    article = soup.find("div", class_="article-body") or soup.find("div", class_="tailwind-article-body")
    if not article:
        article = soup.find("article") or soup.find("div", {"id": "article-body"})
    if not article:
        # Fallback: largest text block
        article = soup.find("body")

    if not article:
        return ""

    text = article.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return "\n".join(lines)


def search_motley_fool_transcripts(company: str, ticker: str, limit: int = 4) -> list[dict]:
    """Search for earnings transcripts on Motley Fool."""
    # Motley Fool transcript URL pattern
    search_url = f"https://www.fool.com/quote/{ticker.lower()}/#earnings-transcripts"
    results = []

    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            print(f"  Motley Fool search returned {resp.status_code}")
            return results

        soup = BeautifulSoup(resp.text, "lxml")
        # Find transcript links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            if "transcript" in text and "earnings" in text and ticker.lower() in href:
                full_url = href if href.startswith("http") else f"https://www.fool.com{href}"
                results.append({"url": full_url, "title": a.get_text(strip=True)})
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"  Motley Fool search error: {e}")

    return results


def search_google_for_transcripts(company: str, ticker: str, limit: int = 4) -> list[dict]:
    """Use a simple search to find transcript URLs."""
    # We'll construct known Motley Fool URL patterns for recent quarters
    results = []
    quarters = [
        ("2026", "Q1", "2026-01"), ("2025", "Q4", "2025-10"),
        ("2025", "Q3", "2025-07"), ("2025", "Q2", "2025-04"),
        ("2025", "Q1", "2025-01"), ("2024", "Q4", "2024-10"),
        ("2024", "Q3", "2024-07"), ("2024", "Q2", "2024-04"),
    ]
    for year, quarter, approx_date in quarters:
        url = f"https://www.fool.com/earnings/call-transcripts/{year}/04/01/{ticker.lower()}-{ticker.lower()}-{quarter.lower()}-{year}-earnings-call-transcript/"
        results.append({"url": url, "title": f"{ticker} {quarter} {year}", "approx_date": approx_date})
        if len(results) >= limit * 2:
            break
    return results


def ingest_earnings_transcripts(ticker: str, company: str, event_type: str,
                                 speaker: str = None, limit: int = 4) -> int:
    """
    Try to ingest the last `limit` earnings transcripts for a company.
    Returns number successfully ingested.
    """
    conn = _get_conn()
    ingested = 0

    print(f"Searching for {company} ({ticker}) earnings transcripts...")

    # Try Motley Fool first
    transcript_urls = search_motley_fool_transcripts(company, ticker, limit)

    if not transcript_urls:
        print(f"  No Motley Fool links found. Trying alternative URLs...")
        # Try known URL patterns from Motley Fool
        # Pattern: https://www.fool.com/earnings/call-transcripts/YYYY/MM/DD/ticker-qqYYYY-earnings-call-transcript/
        alternative_urls = [
            # Netflix recent quarters (approximate dates)
            {"url": f"https://www.fool.com/earnings/call-transcripts/2026/01/22/netflix-nflx-q4-2025-earnings-call-transcript/", "title": "NFLX Q4 2025", "date": "2026-01-22"},
            {"url": f"https://www.fool.com/earnings/call-transcripts/2025/10/17/netflix-nflx-q3-2025-earnings-call-transcript/", "title": "NFLX Q3 2025", "date": "2025-10-17"},
            {"url": f"https://www.fool.com/earnings/call-transcripts/2025/07/18/netflix-nflx-q2-2025-earnings-call-transcript/", "title": "NFLX Q2 2025", "date": "2025-07-18"},
            {"url": f"https://www.fool.com/earnings/call-transcripts/2025/04/18/netflix-nflx-q1-2025-earnings-call-transcript/", "title": "NFLX Q1 2025", "date": "2025-04-18"},
            {"url": f"https://www.fool.com/earnings/call-transcripts/2025/01/22/netflix-nflx-q4-2024-earnings-call-transcript/", "title": "NFLX Q4 2024", "date": "2025-01-22"},
            {"url": f"https://www.fool.com/earnings/call-transcripts/2024/10/18/netflix-nflx-q3-2024-earnings-call-transcript/", "title": "NFLX Q3 2024", "date": "2024-10-18"},
            {"url": f"https://www.fool.com/earnings/call-transcripts/2024/07/19/netflix-nflx-q2-2024-earnings-call-transcript/", "title": "NFLX Q2 2024", "date": "2024-07-19"},
            {"url": f"https://www.fool.com/earnings/call-transcripts/2024/04/19/netflix-nflx-q1-2024-earnings-call-transcript/", "title": "NFLX Q1 2024", "date": "2024-04-19"},
        ] if ticker == "NFLX" else []
        transcript_urls = alternative_urls[:limit * 2]

    for item in transcript_urls:
        if ingested >= limit:
            break

        url = item["url"]
        title = item.get("title", "")
        event_date = item.get("date", "")

        # Extract date from URL if not provided
        if not event_date:
            date_match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
            if date_match:
                event_date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"

        if event_date and already_ingested(conn, "earnings", event_date, event_type):
            print(f"  Skipping {title} (already ingested)")
            continue

        try:
            print(f"  Fetching {title}... ", end="", flush=True)
            text = fetch_motley_fool_transcript(url)

            if not text or len(text.split()) < 200:
                print(f"SKIP (too short: {len(text.split()) if text else 0} words)")
                continue

            word_count = len(text.split())

            if not event_date:
                event_date = "unknown"

            conn.execute(
                """INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("earnings", speaker, event_type, event_date, text, word_count)
            )
            conn.commit()
            ingested += 1
            print(f"Ingested ({word_count:,} words)")
            time.sleep(1.5)

        except Exception as e:
            print(f"ERROR: {e}")
            continue

    conn.close()
    print(f"Done. Ingested {ingested} {ticker} earnings transcripts.")
    return ingested


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True, help="Stock ticker (e.g., NFLX)")
    parser.add_argument("--company", required=True, help="Company name (e.g., Netflix)")
    parser.add_argument("--limit", type=int, default=4, help="Number of transcripts to fetch")
    args = parser.parse_args()

    event_type = f"{args.ticker.lower()}_earnings"
    ingest_earnings_transcripts(args.ticker, args.company, event_type, limit=args.limit)
