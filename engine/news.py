"""Real-time news headlines for context scoring. No API keys required."""

import re
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("news")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}


def get_headlines(query: str, max_results: int = 10) -> list[str]:
    """
    Fetch recent headlines for a query. Uses Google News RSS.
    Returns list of headline strings.
    """
    headlines = []

    # Google News RSS — no API key, no JS, reliable
    try:
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml-xml")
            for item in soup.find_all("item")[:max_results]:
                title = item.find("title")
                if title:
                    headlines.append(title.get_text(strip=True))
    except Exception as e:
        logger.debug(f"Google News RSS failed for '{query}': {e}")

    # Fallback: try without XML parser
    if not headlines:
        try:
            soup = BeautifulSoup(r.text, "lxml")
            for item in soup.find_all("title"):
                text = item.get_text(strip=True)
                if len(text) > 20 and text != "Google News":
                    headlines.append(text)
                    if len(headlines) >= max_results:
                        break
        except Exception:
            pass

    return headlines


def get_earnings_context(ticker: str, company: str = "") -> list[str]:
    """Get headlines relevant to an upcoming earnings call."""
    queries = [
        f"{ticker} earnings",
        f"{ticker} {company} quarterly results" if company else f"{ticker} quarterly",
    ]
    all_headlines = []
    for q in queries:
        all_headlines.extend(get_headlines(q, max_results=5))

    # Deduplicate
    seen = set()
    unique = []
    for h in all_headlines:
        if h.lower() not in seen:
            seen.add(h.lower())
            unique.append(h)

    return unique[:10]


def get_macro_context() -> list[str]:
    """Get current macro headlines for FOMC context scoring."""
    queries = [
        "Federal Reserve interest rates",
        "inflation tariffs economy",
        "FOMC monetary policy",
    ]
    all_headlines = []
    for q in queries:
        all_headlines.extend(get_headlines(q, max_results=5))

    seen = set()
    unique = []
    for h in all_headlines:
        if h.lower() not in seen:
            seen.add(h.lower())
            unique.append(h)

    return unique[:15]


if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "Tesla earnings"
    print(f"Headlines for: {query}")
    for h in get_headlines(query):
        print(f"  - {h}")
