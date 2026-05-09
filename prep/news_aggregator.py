"""News aggregator — pulls last 72h of Fed/macro headlines for context scoring.

Sources (all free, no API key required):
  - Google News RSS (targeted queries for Fed/Powell/FOMC/rate/CPI/NFP)
  - fed.gov press-release RSS (official Fed communications)
  - Reuters Business RSS (macro coverage)

Output: list[dict] of headlines with {title, url, source, published}.
Fed by context_scorer_claude via make_scorer_fn(news_headlines=...).

Key design:
  - Aggressive deduplication (same story breaks on multiple outlets)
  - Time-weighted relevance (news from last 24h counts more)
  - No authentication needed — all endpoints public RSS

Fallback: if all sources fail, returns [] and scorer degrades to heuristic.
"""
from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

_log = logging.getLogger(__name__)


# Tuned query set for FOMC events — each pulls from Google News RSS.
GOOGLE_NEWS_QUERIES = [
    "Jerome Powell Fed",
    "FOMC meeting",
    "Federal Reserve rate",
    "inflation CPI",
    "labor market unemployment",
    "tariff Fed",
    "yield curve Treasury",
    "Fed dot plot",
]


# Official Fed RSS feeds
FED_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.federalreserve.gov/feeds/speeches.xml",
]


# Secondary macro sources
REUTERS_MACRO = "https://www.reuters.com/arc/outboundfeeds/rss-category/business/?outputType=xml"


def _fetch_rss(url: str, timeout: int = 8) -> list[dict]:
    """Parse an RSS or Atom feed → list of {title, link, source, published}."""
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "SovereignBot/1.0"})
        r.raise_for_status()
    except Exception as e:
        _log.warning(f"rss fetch failed {url[:60]}: {e}")
        return []

    out: list[dict] = []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        _log.warning(f"rss parse failed {url[:60]}: {e}")
        return []

    # Handle both RSS 2.0 and Atom
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # RSS 2.0: <rss><channel><item>
    items = root.findall(".//item")
    # Atom: <feed><entry>
    if not items:
        items = root.findall(".//atom:entry", ns) or root.findall(".//entry")

    for it in items[:40]:   # cap per feed
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            link_elem = it.find(".//atom:link", ns) or it.find(".//link")
            if link_elem is not None:
                link = link_elem.get("href", "") or (link_elem.text or "")
        pub = (it.findtext("pubDate") or it.findtext(".//atom:published", ns)
               or it.findtext("published") or "").strip()
        source = re.search(r"https?://([^/]+)/", link)
        source_name = source.group(1) if source else ""
        if title:
            out.append({
                "title": title[:200],
                "url": link[:300],
                "source": source_name,
                "published": pub,
            })
    return out


def _google_news_rss(query: str) -> list[dict]:
    """Pull Google News RSS for a query."""
    q = requests.utils.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return _fetch_rss(url)


def _parse_published(s: str) -> Optional[datetime]:
    """RFC 2822 or ISO 8601 → datetime."""
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _recency_score(pub_dt: Optional[datetime], now: datetime) -> float:
    """Score 1.0 (brand new) → 0.0 (older than 72h)."""
    if pub_dt is None:
        return 0.5
    age_h = (now - pub_dt).total_seconds() / 3600
    if age_h <= 0:
        return 1.0
    if age_h >= 72:
        return 0.0
    return 1.0 - (age_h / 72)


def _dedupe_title(title: str) -> str:
    """Strip common prefixes/outlet suffixes for dedup.
    Handles both en-dash (–) and em-dash (—) in addition to hyphen."""
    t = re.sub(r"\s*[-–—|:]\s*[A-Z][A-Za-z\s\.]{0,30}$", "", title)
    t = re.sub(r"[^\w\s]", "", t).lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def fetch_fed_news(max_age_hours: int = 72,
                    max_headlines: int = 30) -> list[dict]:
    """Return top-N recent Fed/macro headlines, deduped, recency-ranked.

    Uses Google News RSS + fed.gov RSS. Returns empty list if all fail
    (caller should fallback to empty context).
    """
    now = datetime.now(timezone.utc)
    all_items: list[dict] = []

    # Google News
    for q in GOOGLE_NEWS_QUERIES:
        items = _google_news_rss(q)
        all_items.extend(items)

    # Fed official feeds
    for url in FED_FEEDS:
        items = _fetch_rss(url)
        all_items.extend(items)

    if not all_items:
        return []

    # Dedupe by title fingerprint
    seen: dict[str, dict] = {}
    for it in all_items:
        key = _dedupe_title(it.get("title", ""))
        if not key or len(key) < 10:
            continue
        if key in seen:
            continue
        pub_dt = _parse_published(it.get("published", ""))
        if pub_dt:
            age_h = (now - pub_dt).total_seconds() / 3600
            if age_h > max_age_hours or age_h < -1:
                continue
        it["_pub_dt"] = pub_dt
        it["_recency"] = _recency_score(pub_dt, now)
        seen[key] = it

    items = sorted(seen.values(),
                   key=lambda x: -x.get("_recency", 0))[:max_headlines]
    # Remove internal keys before returning
    for i in items:
        i.pop("_pub_dt", None)
        i.pop("_recency", None)
    return items


def as_context_lines(headlines: list[dict], max_n: int = 20) -> list[str]:
    """Format headlines as simple strings for LLM prompt embedding."""
    out = []
    for h in headlines[:max_n]:
        src = f" [{h['source']}]" if h.get("source") else ""
        pub = f" ({h['published'][:16]})" if h.get("published") else ""
        out.append(f"- {h.get('title','')}{src}{pub}")
    return out


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    news = fetch_fed_news(max_age_hours=72, max_headlines=25)
    print(f"\nFetched {len(news)} headlines:\n")
    for h in news:
        print(f"  • {h.get('title','')[:90]}")
        if h.get("published"):
            print(f"      [{h['source']}]  {h['published'][:25]}")
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        print(json.dumps(news, indent=2))
