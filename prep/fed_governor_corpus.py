"""Fed governor speech corpus expander — pulls intermeeting speeches.

Between FOMC meetings (6-week gaps), Fed governors give 10-40 speeches.
These speeches PREDICT what Powell will say — governors coordinate messaging
in advance. Adding them to the corpus gives us:
  1. Richer base-rate signal (more documents mentioning each term)
  2. FRESH language — what's on Fed officials' minds RIGHT NOW
  3. Recency-weighted adjustments (a term hot in recent governor speech is
     highly likely to appear in Powell's presser)

Sources:
  - https://www.federalreserve.gov/feeds/speeches.xml — RSS of all Fed speeches
  - Each speech links to HTML → extractable transcript

Output: inserts rows into sovereign.transcripts table with event_type='fed_speech',
speaker filled with speaker name.

Usage:
    python prep/fed_governor_corpus.py --since 2026-03-18 [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

_log = logging.getLogger(__name__)

SPEECH_FEED = "https://www.federalreserve.gov/feeds/speeches.xml"
DB_PATH = "/root/sovereign/data/sovereign.db"


def _fetch(url: str, timeout: int = 10) -> Optional[str]:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "SovereignBot/1.0"})
        r.raise_for_status()
        return r.text
    except Exception as e:
        _log.warning(f"fetch failed {url[:60]}: {e}")
        return None


def list_speeches_since(since_date: datetime, feed_url: str = SPEECH_FEED) -> list[dict]:
    """Return speeches from federalreserve.gov speeches feed since a date."""
    # Direct requests call — r.content (bytes) parses cleanly; r.text doesn't
    # because the XML has an explicit encoding declaration.
    try:
        r = requests.get(feed_url, timeout=10,
                         headers={"User-Agent": "SovereignBot/1.0"})
        r.raise_for_status()
    except Exception as e:
        _log.warning(f"fetch failed: {e}")
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        _log.warning(f"parse error: {e}")
        return []

    out: list[dict] = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        # Parse date
        pub_dt = None
        try:
            from email.utils import parsedate_to_datetime
            pub_dt = parsedate_to_datetime(pub)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if pub_dt < since_date:
            continue
        # Extract speaker from title — Fed convention: "Speech by Chair Powell on X"
        speaker = extract_speaker(title)
        out.append({
            "title": title[:300],
            "url": link,
            "published": pub_dt.isoformat(),
            "speaker": speaker,
            "event_date": pub_dt.strftime("%Y-%m-%d"),
        })
    return out


def extract_speaker(title: str) -> str:
    """Parse speaker from federalreserve.gov feed titles.

    Primary format (modern): "LastName, Speech Title..."  →  lastname
    Legacy format:           "Speech by Chair Powell..."   →  powell
    """
    # Modern format: "LastName, ..."
    m = re.match(r"^([A-Z][a-z]+)\s*,", title)
    if m:
        return m.group(1).lower()
    # Legacy: "Speech by Chair X"
    m = re.search(r"by\s+(?:Chair|Vice Chair|Governor|Director|President)\s+([A-Z][A-Za-z\-']+)",
                  title)
    if m:
        return m.group(1).lower()
    # Fallback
    m = re.search(r"by\s+([A-Z][A-Za-z\-']+\s+[A-Z][A-Za-z\-']+)", title)
    if m:
        parts = m.group(1).split()
        return parts[-1].lower() if parts else "fed"
    return "fed"


def fetch_speech_body(url: str) -> str:
    """Fetch speech HTML and extract main body text (Fed pages have predictable structure)."""
    html = _fetch(url, timeout=15)
    if not html:
        return ""
    # Strip scripts/styles
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Main content typically in <div id="article"> or <div class="col-xs-12">
    body_match = re.search(
        r"<div[^>]*(?:id=[\"']article[\"']|class=[\"'][^\"']*content[^\"']*)[^>]*>(.*?)</div>\s*<(?:/main|footer)",
        html, re.DOTALL | re.IGNORECASE,
    )
    if body_match:
        body = body_match.group(1)
    else:
        # Fallback: take everything between <main> and </main> or just strip tags
        main_match = re.search(r"<main[^>]*>(.*?)</main>", html, re.DOTALL | re.IGNORECASE)
        body = main_match.group(1) if main_match else html

    # Strip remaining HTML
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\s+", " ", text).strip()
    # Minimum-length sanity: real speeches are ≥ 1000 chars
    return text if len(text) >= 500 else ""


def ingest(since_date: datetime, db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """Fetch + insert new speeches since date. Skips already-ingested URLs."""
    speeches = list_speeches_since(since_date)
    _log.info(f"found {len(speeches)} speeches since {since_date.date()}")

    if dry_run:
        for s in speeches:
            print(f"  [dry] {s['event_date']}  {s['speaker']}  {s['title'][:80]}")
        return {"fetched": len(speeches), "inserted": 0, "skipped": 0, "dry_run": True}

    conn = sqlite3.connect(db_path)
    inserted = 0
    skipped = 0
    errored = 0
    try:
        # Ensure source column indexed as unique-ish
        existing = set(
            r[0] for r in conn.execute(
                "SELECT source FROM transcripts WHERE event_type='fed_speech'"
            ).fetchall()
        )

        for s in speeches:
            if s["url"] in existing:
                skipped += 1
                continue
            body = fetch_speech_body(s["url"])
            if not body:
                errored += 1
                continue
            word_count = len(body.split())
            try:
                conn.execute(
                    """INSERT INTO transcripts
                       (source, speaker, event_type, event_date, raw_text, word_count)
                       VALUES (?, ?, 'fed_speech', ?, ?, ?)""",
                    (s["url"], s["speaker"], s["event_date"], body, word_count),
                )
                conn.commit()
                inserted += 1
                _log.info(f"[+] {s['event_date']}  {s['speaker']}  {word_count}w  {s['title'][:60]}")
            except Exception as e:
                _log.warning(f"insert failed: {e}")
                errored += 1
            time.sleep(0.5)   # be polite to fed.gov
    finally:
        conn.close()

    return {
        "fetched": len(speeches),
        "inserted": inserted,
        "skipped": skipped,
        "errored": errored,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--since", default=None,
                   help="ISO date (YYYY-MM-DD). Default: 45 days ago.")
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    if a.since:
        since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=45)

    r = ingest(since, db_path=a.db, dry_run=a.dry_run)
    print(f"\n{r}")


if __name__ == "__main__":
    main()
