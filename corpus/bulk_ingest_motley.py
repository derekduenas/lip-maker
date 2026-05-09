"""Bulk-ingest Motley Fool earnings transcripts for priority tickers.

Strategy: scrape ticker quote page → extract transcript URLs via regex →
download each transcript page → clean → insert into transcripts table.

Quality gates: min 2000 words, must contain 'thank you', 'operator', 'quarter'.
"""
from __future__ import annotations
import sqlite3, requests, re, time, sys
from bs4 import BeautifulSoup
from datetime import datetime

DB = "/root/sovereign/data/sovereign.db"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# (ticker, exchange, ceo_name) — ceo as speaker tag for the speaker column
PRIORITY_TICKERS = [
    ("NVDA", "nasdaq", "huang"),
    ("WMT",  "nyse",   "mcmillon"),
    ("AAPL", "nasdaq", "cook"),
    ("MSFT", "nasdaq", "nadella"),
    ("GOOGL","nasdaq", "pichai"),
    ("META", "nasdaq", "zuckerberg"),
    ("AMZN", "nasdaq", "jassy"),
    ("JPM",  "nyse",   "dimon"),
]

REQUIRED = ["thank you", "operator", "quarter"]
FORBIDDEN = ["404", "subscribe to read", "premium content", "javascript required"]


def find_transcript_urls(ticker: str, exchange: str) -> list[tuple[str, str]]:
    """Return [(url, event_date)] for ticker."""
    url = f"https://www.fool.com/quote/{exchange}/{ticker.lower()}/"
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return []
    paths = sorted(set(re.findall(r"(/earnings/call-transcripts/[a-z0-9/-]+)", r.text)))
    out = []
    for p in paths:
        m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", p)
        if not m:
            continue
        date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        out.append((f"https://www.fool.com{p}", date))
    return out


def fetch_transcript_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return ""
    soup = BeautifulSoup(r.text, "lxml")
    art = (soup.find("div", class_="article-body")
           or soup.find("div", class_="tailwind-article-body")
           or soup.find("article")
           or soup.find("div", {"id": "article-body"}))
    if not art:
        art = soup.find("body")
    if not art:
        return ""
    text = art.get_text(separator="\n")
    return "\n".join(l.strip() for l in text.split("\n") if l.strip())


def passes_quality(text: str) -> tuple[bool, str]:
    wc = len(text.split())
    if wc < 2000:
        return False, f"too_short ({wc}w)"
    lower = text.lower()
    for f in FORBIDDEN:
        if f in lower:
            return False, f"forbidden:{f}"
    for r in REQUIRED:
        if r not in lower:
            return False, f"missing:{r}"
    return True, "ok"


def already_have(conn, source, event_date, event_type) -> bool:
    return conn.execute(
        "SELECT 1 FROM transcripts WHERE source=? AND event_date=? AND event_type=?",
        (source, event_date, event_type)
    ).fetchone() is not None


def main():
    conn = sqlite3.connect(DB, timeout=10.0)
    total_new = 0
    total_skipped_existing = 0
    total_failed = 0

    for ticker, exchange, ceo in PRIORITY_TICKERS:
        event_type = f"{ticker.lower()}_earnings"
        urls = find_transcript_urls(ticker, exchange)
        print(f"\n=== {ticker} ({ceo}) — {len(urls)} URLs found ===")

        for url, date in urls:
            if already_have(conn, "motley_fool", date, event_type):
                total_skipped_existing += 1
                print(f"  SKIP {date}: already have")
                continue
            try:
                text = fetch_transcript_text(url)
                ok, reason = passes_quality(text)
                if not ok:
                    print(f"  FAIL {date}: {reason}")
                    total_failed += 1
                    continue
                wc = len(text.split())
                conn.execute(
                    """INSERT INTO transcripts
                       (source, speaker, event_type, event_date, raw_text, word_count, speech_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("motley_fool", ceo, event_type, date, text, wc, "earnings_call")
                )
                conn.commit()
                total_new += 1
                print(f"  OK   {date}: {wc:>6}w")
                time.sleep(1.2)  # polite to fool.com
            except Exception as e:
                print(f"  ERR  {date}: {e}")
                total_failed += 1

    conn.close()
    print(f"\n{'='*60}")
    print(f"Summary: +{total_new} new, {total_skipped_existing} existing, {total_failed} failed")


if __name__ == "__main__":
    main()
