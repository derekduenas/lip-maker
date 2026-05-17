"""Hits Daily Double scraper via Playwright (handles JS-rendered Next.js app).

Why Playwright: HDD's content is loaded client-side; static HTML scraping returns
empty. Playwright runs headless Chromium, waits for JS to render, then extracts.

Per agent research 2026-05-03: HDD's 'Building' chart is THE highest-edge data
source for Kalshi music markets — Brandon Fean's $100k+ playbook used HDD
mid-week projections to predict KXALBUMSALES outcomes before market repriced.

PERFORMANCE: ~10-15s per fetch (chromium startup). Cache aggressively (4h TTL).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

HDD_URL = "https://hitsdailydouble.com/sales_plus_streaming.cgi"
PLAYWRIGHT_TIMEOUT_MS = 30000
WAIT_AFTER_LOAD_MS = 4000  # extra time for charts to render


@dataclass
class HDDRow:
    position:        int
    artist:          str
    album:           str
    label:           str
    projected_units: int
    prior_week_units: int
    week_of:         str


def fetch_hdd_top50() -> list[HDDRow]:
    """Scrape current HDD chart via headless Chromium. Returns ranked list."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log.error("playwright not installed — pip install playwright && playwright install chromium")
        return []
    rows: list[HDDRow] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(user_agent="Mozilla/5.0 (compatible; INNAIT-research-bot)")
            page.goto(HDD_URL, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="networkidle")
            # Wait extra for any async JS
            page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
            # Try to get the rendered HTML
            html = page.content()
            browser.close()
        rows = _parse_rendered_html(html)
    except Exception as e:
        _log.warning(f"HDD playwright fetch failed: {e}")
    return rows


def _parse_rendered_html(html: str) -> list[HDDRow]:
    """Parse fully-rendered HDD HTML. Layout varies — try multiple strategies."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    week_of = _extract_week_of(soup)
    rows: list[HDDRow] = []
    # Strategy 1: look for table rows with position numbers
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        row = _try_parse_row(cells, week_of)
        if row:
            rows.append(row)
    if rows:
        return sorted(rows, key=lambda r: r.position)
    # Strategy 2: look for div-based grid layout (common in Next.js)
    # Search for blocks containing position + artist + units
    for div in soup.find_all("div"):
        text = div.get_text(" ", strip=True)
        # Match pattern like "1. Artist - Album 100,000"
        m = re.match(r"^(\d{1,3})[.\s]+(.+?)\s+(\d{1,3}(?:,\d{3})+)\s*$", text)
        if m:
            pos = int(m.group(1))
            mid = m.group(2)
            units = int(m.group(3).replace(",", ""))
            if 1 <= pos <= 100 and units > 100:
                # Try to split artist/album by hyphen
                if " - " in mid:
                    artist, album = mid.split(" - ", 1)
                else:
                    artist, album = mid, ""
                rows.append(HDDRow(
                    position=pos, artist=artist[:80], album=album[:120],
                    label="", projected_units=units, prior_week_units=0,
                    week_of=week_of,
                ))
    return sorted(rows, key=lambda r: r.position)


def _try_parse_row(cells: list[str], week_of: str) -> HDDRow | None:
    """Best-effort row parser — tolerates layout drift."""
    if len(cells) < 4:
        return None
    pos_match = re.match(r"^\s*(\d{1,3})\s*$", cells[0])
    if not pos_match:
        return None
    pos = int(pos_match.group(1))
    if not (1 <= pos <= 200):
        return None
    artist = cells[1] if len(cells) > 1 else ""
    album = cells[2] if len(cells) > 2 else ""
    label = cells[3] if len(cells) > 3 else ""
    nums = []
    for c in cells[3:]:
        m = re.search(r"([\d,]+)", c)
        if m:
            try:
                nums.append(int(m.group(1).replace(",", "")))
            except ValueError:
                pass
    projected = nums[0] if nums else 0
    prior = nums[1] if len(nums) > 1 else 0
    if not artist or not album or projected == 0:
        return None
    return HDDRow(
        position=pos, artist=artist[:80], album=album[:120],
        label=label[:40], projected_units=projected,
        prior_week_units=prior, week_of=week_of,
    )


def _extract_week_of(soup) -> str:
    text = soup.get_text(" ")
    m = re.search(
        r"(?:Week of|Chart Date|Week Ending)\s*[:\-]?\s*([A-Z][a-z]+\s+\d{1,2}[, ]+\d{4})",
        text,
    )
    if m:
        try:
            return datetime.strptime(m.group(1).replace(",", ""), "%B %d %Y").date().isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date().isoformat()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=20)
    a = p.parse_args()
    rows = fetch_hdd_top50()
    print(f"\n=== HDD Building Chart (Playwright) — {len(rows)} entries ===")
    if rows:
        print(f"Week of: {rows[0].week_of}\n")
    print(f"{'#':<4} {'ARTIST':<30} {'ALBUM':<40} {'PROJ':>10} {'PRIOR':>10}")
    print("-" * 100)
    for r in rows[:a.top]:
        print(f"{r.position:<4} {r.artist[:28]:<30} {r.album[:38]:<40} "
              f"{r.projected_units:>10,} {r.prior_week_units:>10,}")
