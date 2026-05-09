"""Trump Speech Corpus Builder — scrapes transcripts from multiple free sources."""

import re
import sqlite3
import sys
import os
import time
import logging

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH
from corpus.validator import validate_transcript, log_rejection

logger = logging.getLogger("trump_corpus")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# Known transcript sources for Trump speeches
# These are real URLs discovered via web search
KNOWN_TRUMP_TRANSCRIPTS = [
    # April 2026
    {"url": "https://singjupost.com/transcript-president-trump-remarks-on-tax-week-in-las-vegas-april-16-2026/",
     "date": "2026-04-16", "title": "Tax Week Las Vegas"},
    # 2026 — curated via web search 2026-04-19 (dates approximate where URL lacks explicit date)
    {"url": "https://singjupost.com/transcript-president-donald-trump-remarks-wef-davos-2026/",
     "date": "2026-01-21", "title": "WEF Davos 2026"},
    {"url": "https://singjupost.com/transcript-president-trump-remarks-national-prayer-breakfast/",
     "date": "2026-02-05", "title": "National Prayer Breakfast 2026"},
    {"url": "https://singjupost.com/president-trumps-remarks-2026-state-of-the-union-address-transcript/",
     "date": "2026-03-04", "title": "State of the Union 2026"},
    {"url": "https://singjupost.com/trump-remarks-on-economy-iran-more-kentucky-event-transcript/",
     "date": "2026-03-11", "title": "Kentucky Event — Economy & Iran"},
    {"url": "https://singjupost.com/transcript-president-trump-remarks-cabinet-meeting-mar-26-2026/",
     "date": "2026-03-26", "title": "Cabinet Meeting Mar 26 2026"},
    {"url": "https://singjupost.com/transcript-president-trump-remarks-at-the-future-investment-initiative/",
     "date": "2026-03-27", "title": "Future Investment Initiative"},
    # 2025 — anchor base_rates on recurring political language
    {"url": "https://singjupost.com/transcript-of-trump-remarks-at-pre-inaugural-victory-maga-rally-in-d-c/",
     "date": "2025-01-19", "title": "Pre-Inaugural Victory Rally DC"},
    {"url": "https://singjupost.com/transcript-of-trump-inauguration-speech-2025/",
     "date": "2025-01-20", "title": "Inauguration Speech 2025"},
    {"url": "https://singjupost.com/full-transcript-president-trump-remarks-at-cpac-2025/",
     "date": "2025-02-22", "title": "CPAC 2025"},
    {"url": "https://singjupost.com/transcript-of-president-trump-remarks-at-liberation-day-event-april-2-2025/",
     "date": "2025-04-02", "title": "Liberation Day"},
    {"url": "https://singjupost.com/transcript-of-president-trumps-speech-at-nrcc-dinner-april-8-2025/",
     "date": "2025-04-08", "title": "NRCC Dinner"},
    {"url": "https://singjupost.com/transcript-of-trump-remarks-on-first-100-days-in-office/",
     "date": "2025-04-29", "title": "First 100 Days Rally"},
    {"url": "https://singjupost.com/transcript-trump-remarks-at-salute-to-america-event-in-iowa-july-3-2025/",
     "date": "2025-07-03", "title": "Salute to America Iowa"},
    # Second batch — added 2026-04-19 to push corpus toward 50
    {"url": "https://singjupost.com/transcript-of-president-trump-remarks-at-world-economic-forum-2025/",
     "date": "2025-01-23", "title": "WEF Davos 2025 (virtual)"},
    {"url": "https://singjupost.com/transcript-trump-netanyahu-press-conference/",
     "date": "2025-02-05", "title": "Trump-Netanyahu Press Conference"},
    {"url": "https://singjupost.com/transcript-of-trumps-university-of-alabama-commencement-speech-2025/",
     "date": "2025-05-01", "title": "Alabama Commencement 2025"},
    {"url": "https://singjupost.com/transcript-of-president-trump-remarks-on-golden-dome-missile-defense-project-may-20-2025/",
     "date": "2025-05-20", "title": "Golden Dome Missile Defense"},
    {"url": "https://singjupost.com/transcript-president-trump-remarks-at-white-house-ai-summit/",
     "date": "2025-07-23", "title": "White House AI Summit"},
    {"url": "https://singjupost.com/transcript-president-trumps-address-to-the-united-nations-general-assembly-9-23-25/",
     "date": "2025-09-23", "title": "UN General Assembly 80th Session"},
    # 2024 campaign-era — anchors base_rates on pre-presidency political language
    {"url": "https://singjupost.com/full-transcript-donald-trumps-speech-at-rnc-2024/",
     "date": "2024-07-18", "title": "RNC Acceptance Speech 2024"},
    {"url": "https://singjupost.com/full-transcript-trumps-maga-rally-at-msg-new-york-city/",
     "date": "2024-10-27", "title": "MSG Rally NYC 2024"},
    {"url": "https://singjupost.com/full-transcript-trumps-maga-rally-in-green-bay-wisconsin/",
     "date": "2024-10-30", "title": "Green Bay Wisconsin Rally 2024"},
]

# Singjupost is the most reliable free source for Trump transcripts
SINGJUPOST_SEARCH = "https://singjupost.com/?s=transcript+president+trump"


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _already_ingested(event_date: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM transcripts WHERE source='political' AND event_type='trump_speech' AND event_date=?",
        (event_date,)
    ).fetchone()
    conn.close()
    return row is not None


def fetch_singjupost_transcript(url: str) -> str | None:
    """Fetch transcript from singjupost.com."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        article = soup.find("div", class_="entry-content") or soup.find("article")
        if not article:
            return None
        text = article.get_text(separator="\n", strip=True)
        return text if len(text.split()) > 500 else None
    except Exception as e:
        logger.debug(f"singjupost fetch failed: {e}")
        return None


def discover_trump_transcripts(max_pages: int = 3) -> list[dict]:
    """Search singjupost for Trump transcript URLs."""
    discovered = []
    for page in range(1, max_pages + 1):
        try:
            url = f"https://singjupost.com/page/{page}/?s=transcript+president+trump"
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                title = a.get_text(strip=True)
                if "transcript" in title.lower() and "trump" in title.lower() and "singjupost.com" in href:
                    # Extract date from URL
                    date_match = re.search(r"(\w+)-(\d{1,2})-(\d{4})", href)
                    if date_match:
                        month_str, day, year = date_match.groups()
                        months = {"january": "01", "february": "02", "march": "03", "april": "04",
                                  "may": "05", "june": "06", "july": "07", "august": "08",
                                  "september": "09", "october": "10", "november": "11", "december": "12"}
                        month = months.get(month_str.lower(), "01")
                        event_date = f"{year}-{month}-{day.zfill(2)}"
                        discovered.append({"url": href, "date": event_date, "title": title[:80]})
            time.sleep(1.5)
        except Exception:
            break

    # Deduplicate by date
    seen = set()
    unique = []
    for d in discovered:
        if d["date"] not in seen:
            seen.add(d["date"])
            unique.append(d)
    return unique


def ingest_all_trump(limit: int = 20) -> dict:
    """Discover and ingest Trump speech transcripts."""
    conn = _get_conn()
    ingested = 0
    rejected = 0

    # Discover transcripts
    logger.info("Discovering Trump transcripts from singjupost...")
    all_sources = list(KNOWN_TRUMP_TRANSCRIPTS)
    discovered = discover_trump_transcripts(max_pages=5)
    logger.info(f"Discovered {len(discovered)} Trump transcripts")

    # Merge known + discovered
    seen_dates = set()
    for s in all_sources:
        seen_dates.add(s["date"])
    for d in discovered:
        if d["date"] not in seen_dates:
            all_sources.append(d)
            seen_dates.add(d["date"])

    # Sort by date descending (most recent first)
    all_sources.sort(key=lambda x: x["date"], reverse=True)

    for source in all_sources[:limit]:
        event_date = source["date"]
        url = source["url"]
        title = source.get("title", "")

        if _already_ingested(event_date):
            continue

        logger.info(f"  Fetching {event_date}: {title[:50]}...")
        text = fetch_singjupost_transcript(url)

        if not text:
            rejected += 1
            continue

        # Validate
        result = validate_transcript(text, "generic", "TRUMP", event_date)
        if not result.passed:
            log_rejection("TRUMP", event_date, "singjupost", result.failure_reason, url)
            rejected += 1
            continue

        # Ingest
        word_count = len(text.split())
        conn.execute(
            """INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("political", "trump", "trump_speech", event_date, text, word_count)
        )
        conn.commit()
        ingested += 1
        logger.info(f"  INGESTED {event_date}: {word_count:,} words")
        time.sleep(2)

    total = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE event_type='trump_speech'"
    ).fetchone()[0]
    conn.close()

    logger.info(f"Trump corpus: ingested={ingested}, rejected={rejected}, total={total}")
    return {"ingested": ingested, "rejected": rejected, "total": total}


def build_trump_frequency_matrix() -> None:
    """Build frequency matrix for Trump speech terms matching Kalshi markets."""
    from engine.frequency import extract_mentions, build_frequency_matrix, build_cooccurrence_matrix

    # Terms from active Kalshi Trump mention markets
    trump_terms = [
        "tariffs", "china", "beautiful", "tremendous", "billion",
        "deal", "fake news", "rigged election", "stolen election",
        "biden", "obama", "barack hussein obama", "sleepy joe",
        "democrat", "republican", "maga", "great", "incredible",
        "stock market", "economy", "inflation", "gas", "gasoline", "oil",
        "nuclear", "iran", "israel", "drill", "drill baby drill",
        "border", "wall", "immigrant", "illegal", "deport",
        "social security", "medicare", "tips", "tax",
        "trillion", "debt", "fraud", "witch hunt",
        "american dream", "god bless", "USA",
        "elon", "musk", "doge", "crypto", "bitcoin",
        "woke", "transgender", "military",
        "epstein", "pelosi", "schumer",
        "casino", "las vegas", "waiter", "waitress",
        "big beautiful bill", "save america",
        "midterm", "election", "vote",
        "hormuz", "hostage", "ceasefire",
    ]

    build_frequency_matrix("trump_speech", trump_terms)
    build_cooccurrence_matrix("trump_speech", trump_terms)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser()
    parser.add_argument("--ingest", action="store_true", help="Discover and ingest Trump transcripts")
    parser.add_argument("--matrix", action="store_true", help="Build frequency matrix")
    parser.add_argument("--limit", type=int, default=20, help="Max transcripts to fetch")
    args = parser.parse_args()

    if args.ingest or not (args.matrix):
        result = ingest_all_trump(limit=args.limit)
        print(f"\nIngested: {result['ingested']}, Total: {result['total']}")

    if args.matrix or args.ingest:
        build_trump_frequency_matrix()
        print("Trump frequency matrix built.")
