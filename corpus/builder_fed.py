"""Fed Ecosystem Corpus Builder — expands beyond FOMC pressers to full Fed document universe."""

import re
import sqlite3
import sys
import os
import time
import logging

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, FOMC_TERMS

logger = logging.getLogger("fed_corpus")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
BASE_URL = "https://www.federalreserve.gov"


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _already_ingested(event_type: str, event_date: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM transcripts WHERE event_type=? AND event_date=?",
        (event_type, event_date)
    ).fetchone()
    conn.close()
    return row is not None


def _fetch_pdf_text(url: str) -> str | None:
    """Download PDF and extract text."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return None
        if "pdf" not in r.headers.get("content-type", "").lower():
            return None
        doc = fitz.open(stream=r.content, filetype="pdf")
        pages = [page.get_text() for page in doc]
        text = "\n".join(pages)
        # Clean page markers
        text = re.sub(r"Page \d+ of \d+", "", text)
        return text.strip()
    except Exception as e:
        logger.debug(f"PDF fetch failed: {url}: {e}")
        return None


def _fetch_html_text(url: str) -> str | None:
    """Fetch and extract text from Fed HTML page."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        article = soup.find("div", {"id": "article"})
        if not article:
            article = soup.find("div", class_="col-xs-12 col-sm-8 col-md-8")
        if not article:
            return None
        text = article.get_text(separator="\n", strip=True)
        return text if len(text.split()) > 200 else None
    except Exception as e:
        logger.debug(f"HTML fetch failed: {url}: {e}")
        return None


def ingest_fomc_minutes(min_year: int = 2020) -> int:
    """Ingest FOMC meeting minutes from Federal Reserve website."""
    conn = _get_conn()
    ingested = 0

    # Minutes are at: /monetarypolicy/fomcminutes{YYYYMMDD}.htm
    # Get dates from our existing FOMC presser dates
    from config.events import FOMC_PRESSER_DATES
    for date_str in FOMC_PRESSER_DATES:
        year = int(date_str[:4])
        if year < min_year:
            continue

        event_date = date_str
        if _already_ingested("fomc_minutes", event_date):
            continue

        date_compact = date_str.replace("-", "")
        url = f"{BASE_URL}/monetarypolicy/fomcminutes{date_compact}.htm"

        logger.info(f"  Minutes {event_date}... ")
        text = _fetch_html_text(url)
        if not text or len(text.split()) < 500:
            continue

        wc = len(text.split())
        conn.execute(
            """INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count, speech_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("fed", "fomc_committee", "fomc_minutes", event_date, text, wc, "minutes")
        )
        conn.commit()
        ingested += 1
        logger.info(f"  INGESTED minutes {event_date} ({wc:,} words)")
        time.sleep(1.5)

    conn.close()
    return ingested


def ingest_fomc_statements(min_year: int = 2020) -> int:
    """Ingest FOMC post-meeting statements."""
    conn = _get_conn()
    ingested = 0

    from config.events import FOMC_PRESSER_DATES
    for date_str in FOMC_PRESSER_DATES:
        year = int(date_str[:4])
        if year < min_year:
            continue

        if _already_ingested("fomc_statement", date_str):
            continue

        date_compact = date_str.replace("-", "")
        url = f"{BASE_URL}/newsevents/pressreleases/monetary{date_compact}a.htm"

        logger.info(f"  Statement {date_str}...")
        text = _fetch_html_text(url)
        if not text or len(text.split()) < 100:
            continue

        wc = len(text.split())
        conn.execute(
            """INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count, speech_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("fed", "fomc_committee", "fomc_statement", date_str, text, wc, "statement")
        )
        conn.commit()
        ingested += 1
        logger.info(f"  INGESTED statement {date_str} ({wc:,} words)")
        time.sleep(1.0)

    conn.close()
    return ingested


def ingest_fed_speeches(min_year: int = 2023, max_pages: int = 5) -> int:
    """Ingest Fed governor speeches from the speeches page."""
    conn = _get_conn()
    ingested = 0

    # Speeches listing page
    for page in range(max_pages):
        url = f"{BASE_URL}/newsevents/speech.htm"
        if page > 0:
            url = f"{BASE_URL}/newsevents/speech/{min_year + page}-speeches.htm"

        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                title = a.get_text(strip=True)

                if "/newsevents/speech/" not in href or len(title) < 20:
                    continue

                # Extract date from URL: speech20260115a.htm
                date_match = re.search(r"speech(\d{8})", href)
                if not date_match:
                    continue

                ds = date_match.group(1)
                event_date = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
                year = int(ds[:4])

                if year < min_year:
                    continue
                if _already_ingested("fed_speech_general", event_date):
                    continue

                full_url = href if href.startswith("http") else BASE_URL + href
                text = _fetch_html_text(full_url)
                if not text or len(text.split()) < 300:
                    continue

                # Detect speaker from title
                speaker = "fed_governor"
                for name in ["powell", "williams", "waller", "cook", "jefferson",
                             "bowman", "kugler", "barr", "logan"]:
                    if name in title.lower():
                        speaker = name
                        break

                wc = len(text.split())
                conn.execute(
                    """INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count, speech_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("fed", speaker, "fed_speech_general", event_date, text, wc, "governor_speech")
                )
                conn.commit()
                ingested += 1
                logger.info(f"  INGESTED speech {event_date} by {speaker} ({wc:,} words)")
                time.sleep(1.5)

        except Exception as e:
            logger.debug(f"Speech page error: {e}")

    conn.close()
    return ingested


def build_fed_ecosystem_matrices() -> None:
    """Build frequency matrices for all Fed document types."""
    from engine.frequency import build_frequency_matrix, build_cooccurrence_matrix

    event_types = ["fomc_minutes", "fomc_statement", "fed_speech_general"]

    conn = _get_conn()
    for et in event_types:
        n = conn.execute("SELECT COUNT(*) FROM transcripts WHERE event_type=?", (et,)).fetchone()[0]
        if n >= 3:
            logger.info(f"Building matrix for {et} (n={n})...")
            build_frequency_matrix(et, FOMC_TERMS)
            build_cooccurrence_matrix(et, FOMC_TERMS)
    conn.close()


def ingest_all() -> dict:
    """Run full Fed ecosystem ingestion."""
    logger.info("=== FED ECOSYSTEM CORPUS BUILD ===")

    results = {}
    results["minutes"] = ingest_fomc_minutes()
    logger.info(f"Minutes ingested: {results['minutes']}")

    results["statements"] = ingest_fomc_statements()
    logger.info(f"Statements ingested: {results['statements']}")

    results["speeches"] = ingest_fed_speeches()
    logger.info(f"Speeches ingested: {results['speeches']}")

    # Build matrices
    build_fed_ecosystem_matrices()

    # Summary
    conn = _get_conn()
    for et in ["fomc_presser", "fomc_minutes", "fomc_statement", "fed_speech_general"]:
        n = conn.execute("SELECT COUNT(*) FROM transcripts WHERE event_type=?", (et,)).fetchone()[0]
        results[et] = n
    total = conn.execute("SELECT COUNT(*) FROM transcripts WHERE source='fed' OR source='fomc'").fetchone()[0]
    results["total_fed"] = total
    conn.close()

    logger.info(f"=== FED ECOSYSTEM: {total} total documents ===")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    results = ingest_all()
    print(f"\nFed Ecosystem Corpus:")
    for k, v in results.items():
        print(f"  {k}: {v}")
