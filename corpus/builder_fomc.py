"""FOMC Corpus Builder — downloads Federal Reserve press conference transcript PDFs."""

import re
import sqlite3
import time
import sys
import os
import io

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH

BASE_URL = "https://www.federalreserve.gov"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

POWELL_START = "2018-02-05"


def get_presser_transcript_pdfs() -> list[dict]:
    """Find all press conference transcript PDF URLs from Fed calendar pages."""
    results = []

    # Current calendar + historical year pages
    calendar_pages = [f"{BASE_URL}/monetarypolicy/fomccalendars.htm"]
    for year in range(2015, 2027):
        calendar_pages.append(f"{BASE_URL}/monetarypolicy/fomchistorical{year}.htm")

    for page_url in calendar_pages:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")

            # Each FOMC meeting page has a link to the presser transcript
            # We need to find the individual meeting pages first
            for link in soup.find_all("a", href=True):
                href = link["href"]
                # Look for presser page links
                if "presconf" in href and href.endswith(".htm"):
                    date_match = re.search(r"presconf(\d{8})", href)
                    if date_match:
                        date_str = date_match.group(1)
                        event_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                        meeting_url = href if href.startswith("http") else BASE_URL + href
                        results.append({"meeting_url": meeting_url, "event_date": event_date, "date_str": date_str})

                # Also directly look for PDF links to presser transcripts
                if "FOMCpresconf" in href and href.endswith(".pdf"):
                    date_match = re.search(r"presconf(\d{8})", href)
                    if date_match:
                        date_str = date_match.group(1)
                        event_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                        pdf_url = href if href.startswith("http") else BASE_URL + href
                        results.append({"pdf_url": pdf_url, "event_date": event_date, "date_str": date_str})

        except Exception as e:
            print(f"  Warning: could not fetch {page_url}: {e}")

    # Deduplicate by date, prefer direct PDF links
    by_date = {}
    for r in results:
        d = r["event_date"]
        if d not in by_date:
            by_date[d] = r
        elif "pdf_url" in r:
            by_date[d].update(r)

    unique = sorted(by_date.values(), key=lambda x: x["event_date"])
    return unique


def find_pdf_url_from_meeting_page(meeting_url: str) -> str | None:
    """Given a meeting page URL, find the transcript PDF link."""
    try:
        resp = requests.get(meeting_url, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True).lower()
            if "FOMCpresconf" in href and href.endswith(".pdf"):
                return href if href.startswith("http") else BASE_URL + href
            if "press conference transcript" in text and href.endswith(".pdf"):
                return href if href.startswith("http") else BASE_URL + href
    except Exception:
        pass
    return None


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract clean text from a transcript PDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page in doc:
        pages.append(page.get_text())
    text = "\n".join(pages)

    # Clean up: remove page headers/footers
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        # Skip page markers
        if re.match(r"^Page \d+ of \d+$", line):
            continue
        if line.startswith("FINAL") and len(line) < 10:
            continue
        if line:
            lines.append(line)
    return "\n".join(lines)


def get_speaker_for_date(event_date: str) -> str:
    if event_date >= POWELL_START:
        return "powell"
    return "yellen"


def already_ingested(conn: sqlite3.Connection, event_date: str) -> bool:
    row = conn.execute(
        "SELECT id FROM transcripts WHERE source='fomc' AND event_date=?",
        (event_date,)
    ).fetchone()
    return row is not None


def ingest_all(min_year: int = 2015):
    """Main entry: fetch all FOMC press conference transcripts and store in DB."""
    conn = sqlite3.connect(DB_PATH)

    # First, clear any bad data from previous runs (word_count < 500 = not a real transcript)
    conn.execute("DELETE FROM transcripts WHERE source='fomc' AND word_count < 500")
    conn.commit()

    print("Fetching FOMC press conference URLs from Fed website...")
    entries = get_presser_transcript_pdfs()
    print(f"Found {len(entries)} press conference entries")

    ingested = 0
    skipped = 0
    failed = 0

    for item in entries:
        event_date = item["event_date"]
        date_str = item.get("date_str", event_date.replace("-", ""))

        if int(event_date[:4]) < min_year:
            continue

        if already_ingested(conn, event_date):
            skipped += 1
            continue

        # Get the PDF URL
        pdf_url = item.get("pdf_url")
        if not pdf_url and "meeting_url" in item:
            pdf_url = find_pdf_url_from_meeting_page(item["meeting_url"])

        if not pdf_url:
            # Try the standard URL pattern
            pdf_url = f"{BASE_URL}/mediacenter/files/FOMCpresconf{date_str}.pdf"

        try:
            print(f"  Fetching {event_date}... ", end="", flush=True)
            resp = requests.get(pdf_url, headers=HEADERS, timeout=60)

            if resp.status_code != 200:
                print(f"SKIP (HTTP {resp.status_code})")
                failed += 1
                continue

            if "pdf" not in resp.headers.get("content-type", "").lower():
                print(f"SKIP (not a PDF)")
                failed += 1
                continue

            text = extract_text_from_pdf(resp.content)
            word_count = len(text.split())

            if word_count < 500:
                print(f"SKIP (too short: {word_count} words)")
                failed += 1
                continue

            speaker = get_speaker_for_date(event_date)

            conn.execute(
                """INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("fomc", speaker, "fomc_presser", event_date, text, word_count)
            )
            conn.commit()
            ingested += 1
            print(f"Ingested ({word_count:,} words)")

            time.sleep(1.0)

        except Exception as e:
            print(f"ERROR: {e}")
            failed += 1
            continue

    total = conn.execute("SELECT COUNT(*) FROM transcripts WHERE source='fomc'").fetchone()[0]
    conn.close()
    print(f"\nDone. Ingested: {ingested}, Skipped (already in DB): {skipped}, Failed: {failed}")
    print(f"Total FOMC transcripts in DB: {total}")


if __name__ == "__main__":
    ingest_all()
