"""Hits Daily Double Top 50 album sales scraper.

WHY THIS IS THE HIGHEST-VALUE DATA SOURCE (per agent research 2026-05-03):
  Kalshi's KXALBUMSALES markets settle on Hits Daily Double's Top 50, NOT
  Luminate or Billboard. HDD posts mid-week "Building" projections (Mon + Wed)
  before final numbers. Brandon Fean ($100k+ documented winner) and others
  have used this data structurally — it's the most exploitable inefficiency
  in Kalshi music markets right now.

OUTPUT:
  Per-album projections + entry into the Top 50 chart. Each row:
    artist, album, projected_units, prior_week_units, position, week_of

CACHING:
  HDD is updated 2x/week. Cache 4h locally to avoid hammering them. Be polite
  with delays + a real User-Agent.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup


HDD_URL = "https://hitsdailydouble.com/sales_plus_streaming.cgi"
USER_AGENT = "Mozilla/5.0 (compatible; INNAIT-research-bot)"
REQUEST_TIMEOUT = 30
POLITENESS_DELAY_SEC = 5

_log = logging.getLogger(__name__)


@dataclass
class HDDRow:
    position:        int
    artist:          str
    album:           str
    label:           str
    projected_units: int            # building number (mid-week projection)
    prior_week_units: int           # last week actual
    week_of:         str            # ISO date for chart-week-ending-thursday


def fetch_hdd_top50(timeout: int = REQUEST_TIMEOUT) -> list[HDDRow]:
    """Scrape current HDD Building chart. Returns ranked list."""
    try:
        time.sleep(POLITENESS_DELAY_SEC)
        r = requests.get(HDD_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        _log.warning(f"HDD fetch failed: {e}")
        return []
    return _parse_hdd_html(r.text)


def _parse_hdd_html(html: str) -> list[HDDRow]:
    """Best-effort parse. HDD's HTML is non-semantic; rows are typically in
    a single big table. We'll match flexibly to survive layout changes."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[HDDRow] = []
    week_of = _extract_week_of(soup)
    # Strategy: find every <tr> with cells that look like (number, name, name, label, num, num)
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        # Position is typically the first numeric cell
        pos_match = re.match(r"^\s*(\d{1,3})\s*$", cells[0])
        if not pos_match:
            continue
        position = int(pos_match.group(1))
        if not (1 <= position <= 200):
            continue
        # Artist + album typically next two cells
        artist = cells[1] if len(cells) > 1 else ""
        album = cells[2] if len(cells) > 2 else ""
        label = cells[3] if len(cells) > 3 else ""
        # Find first two numeric cells after the title block (projected, prior)
        nums = []
        for c in cells[3:]:
            m = re.search(r"([\d,]+)", c)
            if m:
                try:
                    nums.append(int(m.group(1).replace(",", "")))
                except ValueError:
                    pass
            if len(nums) >= 2:
                break
        projected = nums[0] if nums else 0
        prior = nums[1] if len(nums) > 1 else 0
        if not artist or not album or projected == 0:
            continue
        rows.append(HDDRow(
            position=position, artist=artist[:80], album=album[:120],
            label=label[:40], projected_units=projected,
            prior_week_units=prior, week_of=week_of,
        ))
    rows.sort(key=lambda r: r.position)
    return rows


def _extract_week_of(soup: BeautifulSoup) -> str:
    """Pull chart-week date from page. Falls back to today's date."""
    text = soup.get_text(" ")
    m = re.search(
        r"(?:Week of|Chart Date|Week Ending)\s*[:\-]?\s*([A-Z][a-z]+\s+\d{1,2}[, ]+\d{4})",
        text,
    )
    if m:
        try:
            d = datetime.strptime(m.group(1).replace(",", ""), "%B %d %Y")
            return d.date().isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date().isoformat()


def normalize_artist_for_match(name: str) -> str:
    """Normalize artist name for matching against Kalshi market titles.
    Strips features, casing, special chars."""
    s = name.lower()
    # Strip 'feat.', 'featuring', '&', 'and', etc.
    s = re.sub(r"\s+(feat\.?|featuring|with|&|and)\s+.+$", "", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(s.split())


def normalize_album_for_match(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", s)  # strip parentheticals
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(s.split())


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=20)
    a = p.parse_args()
    rows = fetch_hdd_top50()
    print(f"\n=== HDD Building Chart — {len(rows)} entries scraped ===")
    if rows:
        print(f"Week of: {rows[0].week_of}\n")
    print(f"{'#':<4} {'ARTIST':<30} {'ALBUM':<40} {'PROJ':>10} {'PRIOR':>10}")
    print("-" * 100)
    for r in rows[:a.top]:
        print(f"{r.position:<4} {r.artist[:28]:<30} {r.album[:38]:<40} "
              f"{r.projected_units:>10,} {r.prior_week_units:>10,}")
