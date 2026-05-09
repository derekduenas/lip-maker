"""Autonomous corpus builder. Called by morning_scan when a market has no corpus."""

import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH
from corpus.sources import EARNINGS_SOURCES, get_speaker, get_event_type, TICKER_SPEAKER_MAP
from corpus.validator import validate_transcript, log_rejection
from config.watchlist import add_to_watchlist

logger = logging.getLogger("auto_ingest")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MIN_CORPUS_QUALITY_SCORE = 0.45
AUTO_INGEST_QUARTERS_BACK = 8
REQUEST_DELAY = 2.5


def auto_ingest(ticker: str, event_date: date = None) -> dict:
    """
    Master function. Fetches, validates, ingests transcripts, builds matrix.
    Called by morning_scan when no corpus exists for a ticker.
    """
    ticker = ticker.upper()
    speaker = get_speaker(ticker)
    event_type = get_event_type(ticker)

    logger.info(f"AUTO-INGEST: {ticker} | speaker={speaker}")

    if speaker == "unknown":
        logger.warning(f"{ticker}: speaker unknown — add to TICKER_SPEAKER_MAP")
        add_to_watchlist(event_type, ticker, "UNKNOWN_SPEAKER", 0.0, 0)
        return _result(ticker, "unknown_speaker", message=f"Speaker unknown for {ticker}")

    existing_n = _get_existing_count(event_type)
    quarters = _get_missing_quarters(event_type)

    if not quarters:
        return _result(ticker, "success", corpus_n=existing_n,
                       ready=existing_n >= 3, message=f"Corpus complete (n={existing_n})")

    ingested = 0
    rejected = 0
    failed = 0
    quality_scores = []

    for q in quarters:
        quarter_str = q["quarter"]
        logger.info(f"{ticker} {quarter_str}: fetching...")

        text = _fetch_transcript(ticker, q["year"], q["q_num"])

        if text is None:
            failed += 1
            continue

        result = validate_transcript(text, "motley_fool", ticker, quarter_str)

        if not result.passed:
            log_rejection(ticker, quarter_str, "motley_fool", result.failure_reason)
            rejected += 1
            continue

        if result.quality_score < MIN_CORPUS_QUALITY_SCORE:
            log_rejection(ticker, quarter_str, "motley_fool",
                          f"Quality too low: {result.quality_score:.2f}")
            rejected += 1
            continue

        if result.warnings:
            for w in result.warnings:
                logger.warning(f"{ticker} {quarter_str}: {w}")

        # Ingest
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """INSERT INTO transcripts (source, speaker, event_type, event_date, raw_text, word_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("earnings", speaker, event_type, q["approx_date"], text, result.word_count)
            )
            conn.commit()
            conn.close()
            ingested += 1
            quality_scores.append(result.quality_score)
            logger.info(f"{ticker} {quarter_str}: INGESTED ({result.word_count:,} words, q={result.quality_score:.2f})")
        except Exception as e:
            logger.error(f"{ticker} {quarter_str}: DB error: {e}")
            rejected += 1

        time.sleep(REQUEST_DELAY)

    # Rebuild frequency matrix if we ingested anything
    final_n = _get_existing_count(event_type)
    if ingested > 0 and final_n >= 3:
        _rebuild_matrix(ticker, speaker, event_type)

    ready = final_n >= 3
    status = "success" if ingested > 0 else "failed"

    # Log to ingestion_log table
    _log_ingestion(ticker, event_type, "auto", len(quarters), ingested, rejected,
                   existing_n, final_n, ready)

    return _result(ticker, status, ingested=ingested, rejected=rejected,
                   failed_fetch=failed, corpus_n=final_n, ready=ready,
                   quality_scores=quality_scores,
                   message=f"Ingested {ingested}. Corpus n={final_n}. {'Ready.' if ready else f'Need {3-final_n} more.'}")


def _result(ticker, status, **kwargs):
    return {
        "ticker": ticker, "status": status,
        "ingested": kwargs.get("ingested", 0),
        "rejected": kwargs.get("rejected", 0),
        "failed_fetch": kwargs.get("failed_fetch", 0),
        "corpus_n": kwargs.get("corpus_n", 0),
        "ready_to_trade": kwargs.get("ready", False),
        "quality_scores": kwargs.get("quality_scores", []),
        "message": kwargs.get("message", ""),
    }


def _get_existing_count(event_type: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchone()[0]
    conn.close()
    return n


def _get_missing_quarters(event_type: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    existing = {r[0] for r in conn.execute(
        "SELECT event_date FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchall()}
    conn.close()

    today = date.today()
    quarters = []
    for i in range(AUTO_INGEST_QUARTERS_BACK):
        q_month = today.month - (i * 3)
        q_year = today.year
        while q_month <= 0:
            q_month += 12
            q_year -= 1
        q_num = (q_month - 1) // 3 + 1
        approx = date(q_year, min(q_num * 3, 12), 15)
        quarter_str = f"Q{q_num} {q_year}"

        already_have = any(
            abs((date.fromisoformat(d) - approx).days) < 45
            for d in existing if d
        )
        if not already_have:
            quarters.append({
                "quarter": quarter_str, "year": q_year, "q_num": q_num,
                "approx_date": approx.isoformat(),
            })

    return quarters


# Company name map — Motley Fool URLs use company names, not just tickers
TICKER_COMPANY_MAP = {
    "TSLA": "tesla", "NVDA": "nvidia", "AAPL": "apple", "META": "meta-platforms",
    "NFLX": "netflix", "AMZN": "amazon", "MSFT": "microsoft", "GOOGL": "alphabet",
    "RDDT": "reddit", "CMG": "chipotle-mexican-grill", "AMD": "amd",
    "INTC": "intel", "BA": "boeing", "CRM": "salesforce", "UBER": "uber",
    "TFC": "truist", "PGR": "progressive", "PEP": "pepsico", "KO": "coca-cola",
    "JPM": "jpmorgan-chase", "GS": "goldman-sachs", "MCD": "mcdonalds",
    "PG": "procter-gamble", "UAL": "united-airlines", "ALLY": "ally-financial",
    "GEV": "ge-vernova", "ALK": "alaska-air", "FDX": "fedex",
}


def _fetch_transcript(ticker: str, year: int, q_num: int) -> str | None:
    """
    Source waterfall: try multiple sources in priority order.
    1. Motley Fool (via Google-discovered URL patterns)
    2. Insider Monkey (free full transcripts)
    3. Direct Motley Fool URL guessing
    """
    text = _fetch_motley_fool(ticker, year, q_num)
    if text and len(text.split()) > 1000:
        return text

    text = _fetch_insider_monkey(ticker, year, q_num)
    if text and len(text.split()) > 1000:
        return text

    return None


def _fetch_motley_fool(ticker: str, year: int, q_num: int) -> str | None:
    """Fetch from Motley Fool using multiple slug patterns + date scanning."""
    company = TICKER_COMPANY_MAP.get(ticker, ticker.lower())
    ticker_lower = ticker.lower()

    # Earnings for Qn of year Y are typically reported:
    # Q1 -> Apr/May of Y, Q2 -> Jul of Y, Q3 -> Oct of Y, Q4 -> Jan of Y+1
    report_configs = {
        1: (0, [4, 5]),      # Q1 reported Apr-May same year
        2: (0, [7, 8]),      # Q2 reported Jul-Aug same year
        3: (0, [10, 11]),    # Q3 reported Oct-Nov same year
        4: (1, [1, 2]),      # Q4 reported Jan-Feb next year
    }
    year_offset, months = report_configs.get(q_num, (0, []))
    report_year = year + year_offset

    # Slug patterns — most common first for speed
    slug_patterns = [
        f"{company}-{ticker_lower}-q{q_num}-{year}-earnings-call-transcript",
        f"{company}-{ticker_lower}-q{q_num}-{year}-earnings-call-transcri",
        f"{company}-q{q_num}-{year}-earnings-call-transcript",
        f"{ticker_lower}-{ticker_lower}-q{q_num}-{year}-earnings-call-transcript",
        f"{ticker_lower}-q{q_num}-{year}-earnings-call-transcript",
    ]

    # Most common earnings release days (ordered by frequency)
    # Earnings typically drop on Tue-Thu, days 14-28
    priority_days = [22, 21, 23, 18, 17, 19, 20, 24, 25, 15, 16, 26, 27, 28, 29, 14]

    session = requests.Session()
    session.headers.update(HEADERS)

    for slug in slug_patterns:
        for month in months:
            for day in priority_days:
                url = f"https://www.fool.com/earnings/call-transcripts/{report_year}/{month:02d}/{day:02d}/{slug}/"
                try:
                    r = session.head(url, timeout=4, allow_redirects=True)
                    if r.status_code == 200:
                        page = session.get(url, timeout=15)
                        if page.status_code == 200 and len(page.text) > 10000:
                            text = _extract_article_text(page.text)
                            if text and len(text.split()) > 1000:
                                return text
                except Exception:
                    continue
        # If first slug pattern checked all dates and found nothing, move to next slug

    return None


def _fetch_insider_monkey(ticker: str, year: int, q_num: int) -> str | None:
    """Fetch from Insider Monkey — free full transcripts, no paywall."""
    try:
        search_url = f"https://www.insidermonkey.com/blog/tag/{ticker.lower()}-earnings-call-transcript/"
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        target = f"q{q_num} {year}".lower()

        for a in soup.find_all("a", href=True):
            text_content = a.get_text(strip=True).lower()
            href = a["href"]
            if (f"q{q_num}" in text_content and str(year) in text_content
                    and "transcript" in text_content and ticker.lower() in text_content):
                full_url = href if href.startswith("http") else f"https://www.insidermonkey.com{href}"
                time.sleep(1.5)
                page = requests.get(full_url, headers=HEADERS, timeout=15)
                if page.status_code == 200:
                    return _extract_article_text(page.text)

    except Exception as e:
        logger.debug(f"Insider Monkey fetch failed for {ticker} Q{q_num} {year}: {e}")

    return None


def _extract_article_text(html: str) -> str | None:
    """Extract article body text from any transcript page."""
    soup = BeautifulSoup(html, "lxml")

    # Try common article body selectors
    article = (
        soup.find("div", class_="tailwind-article-body")
        or soup.find("div", class_="article-body")
        or soup.find("div", class_="single-post-content")
        or soup.find("article")
    )

    if not article:
        # Fallback: find largest text block
        for div in soup.find_all("div"):
            text = div.get_text()
            if len(text) > 5000 and any(kw in text.lower() for kw in ["earnings", "quarter", "revenue"]):
                article = div
                break

    if not article:
        return None

    text = article.get_text(separator="\n", strip=True)
    return _clean_text(text)


def _clean_text(raw: str) -> str:
    """Standardize transcript text."""
    if not raw:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", raw)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _rebuild_matrix(ticker: str, speaker: str, event_type: str) -> None:
    """Rebuild frequency + co-occurrence matrices after new ingestion."""
    try:
        from engine.speaker import build_speaker_frequency_matrix, SPEAKER_TERM_LISTS
        from engine.frequency import build_frequency_matrix, build_cooccurrence_matrix

        terms = SPEAKER_TERM_LISTS.get(speaker)
        if terms:
            build_speaker_frequency_matrix(speaker, ticker)
        else:
            # No predefined term list — build from Kalshi market terms + common terms
            terms = _get_kalshi_terms_for_ticker(ticker)
            if not terms:
                terms = _extract_top_terms(event_type)
            if terms:
                build_frequency_matrix(event_type, terms)
                build_cooccurrence_matrix(event_type, terms)
        logger.info(f"{ticker}: matrix rebuilt")
    except Exception as e:
        logger.error(f"{ticker}: matrix rebuild failed: {e}")


def _get_kalshi_terms_for_ticker(ticker: str) -> list[str]:
    """
    Fetch the actual terms Kalshi is listing for this ticker's mention markets.
    These are the terms we need base rates for — not generic extracted terms.
    """
    try:
        from engine.scanner import KalshiClient, classify_market
        client = KalshiClient()
        markets = client.get_mention_markets()

        terms = set()
        event_type = f"{ticker.lower()}_earnings"
        for m in markets:
            c = classify_market(m)
            if c["event_type"] == event_type and c.get("term"):
                terms.add(c["term"].lower())

        # Also add common earnings terms that show up across all calls
        common = ["tariffs", "inflation", "recession", "AI", "headwinds",
                  "credit", "China", "acquisition", "buyback", "dividend",
                  "guidance", "demand", "margin", "revenue", "competition"]
        terms.update(common)

        logger.info(f"{ticker}: extracted {len(terms)} terms from Kalshi markets + common terms")
        return list(terms)
    except Exception as e:
        logger.debug(f"Could not get Kalshi terms for {ticker}: {e}")
        return []


def _extract_top_terms(event_type: str, top_n: int = 30) -> list[str]:
    """Extract frequent meaningful terms from transcripts when no predefined list exists."""
    from collections import Counter
    stopwords = {
        "the", "and", "that", "this", "with", "for", "are", "was", "were",
        "have", "has", "had", "been", "will", "would", "could", "should",
        "our", "we", "you", "they", "their", "not", "but", "also", "just",
        "about", "very", "going", "think", "really", "well", "more", "some",
        "what", "which", "when", "where", "how", "from", "into", "than",
    }
    conn = sqlite3.connect(DB_PATH)
    texts = [r[0] for r in conn.execute(
        "SELECT raw_text FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchall()]
    conn.close()

    counter = Counter()
    for text in texts:
        words = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
        counter.update(w for w in words if w not in stopwords)

    return [t for t, _ in counter.most_common(top_n)]


def _log_ingestion(ticker, event_type, triggered_by, found, ingested,
                   rejected, n_before, n_after, ready):
    """Log ingestion attempt to ingestion_log table."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO ingestion_log
               (ticker, event_type, triggered_by, quarters_found, quarters_ingested,
                quarters_rejected, corpus_n_before, corpus_n_after, ready_to_trade)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, event_type, triggered_by, found, ingested,
             rejected, n_before, n_after, ready)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # table may not exist yet — non-critical


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Sovereign Auto-Ingest")
    parser.add_argument("--ticker", help="Ingest specific ticker")
    parser.add_argument("--from-watchlist", action="store_true",
                        help="Ingest all watchlist tickers with known speakers")
    parser.add_argument("--log", action="store_true", help="Show ingestion log")
    args = parser.parse_args()

    if args.ticker:
        result = auto_ingest(args.ticker)
        print(f"\nResult: {result['status'].upper()}")
        print(f"Ingested: {result['ingested']} quarters")
        print(f"Corpus n: {result['corpus_n']}")
        print(f"Ready: {result['ready_to_trade']}")
        print(f"Message: {result['message']}")

    elif args.from_watchlist:
        try:
            with open("data/watchlist.json") as f:
                watchlist = json.load(f)
            tickers = list({e["ticker"] for e in watchlist if get_speaker(e["ticker"]) != "unknown"})
            print(f"Ingesting {len(tickers)} tickers...")
            for t in tickers:
                r = auto_ingest(t)
                print(f"  {t}: {r['status']} (ingested={r['ingested']}, n={r['corpus_n']})")
        except FileNotFoundError:
            print("No watchlist found.")

    elif args.log:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT ticker, quarters_ingested, corpus_n_after, ready_to_trade, triggered_by, triggered_at FROM ingestion_log ORDER BY triggered_at DESC LIMIT 20"
            ).fetchall()
            print(f"\nIngestion log ({len(rows)} entries):")
            for r in rows:
                ready = "Y" if r[3] else "N"
                print(f"  {r[4]:>12} {r[0]:>6} | +{r[1]}q | n={r[2]} | ready={ready} | {str(r[5])[:16]}")
        except Exception:
            print("ingestion_log table not yet created.")
        conn.close()
