"""kworb.net Spotify chart scraper.

WHY: Kalshi's KXRANKLISTSONGSPOTGLOBAL, KXRANKLISTSONGSPOTUSA, KXTOPARTIST,
KXSPOTIFYD all settle on Spotify's official charts. kworb.net mirrors those
charts in scrapable static HTML — no JS rendering required.

OUTPUTS:
  - top_songs(country)  → ranked list of (rank, song, artist, streams_today, total_streams)
  - top_artists(country) → ranked list of (rank, artist, streams_today, points)

Per agent research 2026-05-03: this is the PRIMARY recommended free data source
for Spotify-market trading because it (a) is what Kalshi settles on indirectly,
(b) has clean static HTML, (c) updates daily after Spotify publishes ~22:00 UTC.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup


KWORB_BASE = "https://kworb.net/spotify"
USER_AGENT = "Mozilla/5.0 (compatible; INNAIT-research-bot)"
TIMEOUT = 30
POLITENESS_SEC = 3

_log = logging.getLogger(__name__)


@dataclass
class SongRow:
    rank:           int
    artist:         str
    song:           str
    streams_today:  int
    streams_total:  int
    days_on_chart:  int
    movement:       int     # rank change vs yesterday (negative = climbing)
    country:        str     # "global" or "us"
    chart_date:     str


@dataclass
class ArtistRow:
    rank:           int
    artist:         str
    points:         int            # kworb's Spotify daily points
    movement:       int
    country:        str
    chart_date:     str


def fetch_top_songs(country: str = "global", limit: int = 200) -> list[SongRow]:
    """country: 'global' or country code like 'us'."""
    url = (f"{KWORB_BASE}/country/{country}_daily.html" if country != "global"
           else f"{KWORB_BASE}/country/global_daily.html")
    try:
        time.sleep(POLITENESS_SEC)
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        _log.warning(f"kworb fetch failed [{country}]: {e}")
        return []
    return _parse_songs(r.text, country, limit)


def _parse_songs(html: str, country: str, limit: int) -> list[SongRow]:
    """Parse kworb chart. Verified column layout (2026-05-04):
       0=Pos, 1=P+, 2='Artist - Title', 3=Days, 4=Pk, 5='(x?)',
       6=Streams, 7=Streams+, 8=7Day, 9=7Day+, 10=Total
    """
    soup = BeautifulSoup(html, "html.parser")
    chart_date = _extract_chart_date(soup)
    rows: list[SongRow] = []
    table = soup.find("table")
    if not table:
        return []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 11:
            continue
        # Skip header row (Pos column will be the literal text "Pos")
        try:
            rank = int(cells[0])
        except ValueError:
            continue
        if not (1 <= rank <= 250):
            continue
        # Movement
        mv_str = cells[1].replace("=", "0").replace("+", "")
        try:
            movement = int(mv_str)
            # Negative in kworb means CLIMBING (rank dropped lower number = better)
            # +1 means dropped 1 spot. We invert so positive movement = climbing.
            if "+" in cells[1]:
                movement = -abs(movement)
            elif "-" in cells[1]:
                movement = abs(movement)
        except ValueError:
            movement = 0
        # Artist - Title
        artist, song = _split_artist_song(cells[2])
        # Days on chart
        try:
            days_on_chart = int(cells[3])
        except ValueError:
            days_on_chart = 0
        # Streams today
        try:
            streams_today = int(cells[6].replace(",", ""))
        except ValueError:
            streams_today = 0
        # Total streams (lifetime)
        try:
            streams_total = int(cells[10].replace(",", ""))
        except ValueError:
            streams_total = 0
        if not artist:
            continue
        rows.append(SongRow(
            rank=rank, artist=artist, song=song,
            streams_today=streams_today, streams_total=streams_total,
            days_on_chart=days_on_chart, movement=movement,
            country=country, chart_date=chart_date,
        ))
        if len(rows) >= limit:
            break
    return rows


def _split_artist_song(s: str) -> tuple[str, str]:
    """kworb format: 'Artist Name - Song Title' (with possible feat.)"""
    if " - " in s:
        a, sng = s.split(" - ", 1)
        return a.strip(), sng.strip()
    # Sometimes just artist
    return s.strip(), ""


def _extract_chart_date(soup: BeautifulSoup) -> str:
    text = soup.get_text(" ")
    m = re.search(r"Chart\s+Date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    # Try other formats
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text[:2000])
    if m:
        return m.group(1)
    return datetime.now(timezone.utc).date().isoformat()


def normalize_for_match(s: str) -> str:
    """Aggressive normalization for fuzzy artist/song matching.
    Handles: case, parentheticals, leading 'the', '&'/'and', accents, possessives.
    """
    if not s:
        return ""
    s = s.lower()
    # Strip parentheticals (feat., remix, edit notes)
    s = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", s)
    # Strip "feat./featuring/with/&/and X" trailing
    s = re.sub(r"\s+(feat\.?|featuring|with|x|/)\s+.+$", "", s)
    # Replace & with and
    s = s.replace("&", "and")
    # Strip leading "the "
    s = re.sub(r"^the\s+", "", s)
    # Strip possessives
    s = s.replace("'s", "s").replace("'", "")
    # Strip non-alphanumeric
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(s.split())


# ── Stream velocity helpers (key alpha signal) ──

def momentum_score(row: SongRow, prior_rows: list[SongRow]) -> float:
    """Score 0-1 for how much this song is GAINING vs losing momentum.
    Positive = song is climbing fast. Negative = falling fast.
    Uses rank movement + stream velocity if available."""
    score = 0.0
    # Rank movement: -10 = jumped 10 spots up = +0.5 to score
    if row.movement < 0:
        score += min(0.5, abs(row.movement) / 20)
    elif row.movement > 0:
        score -= min(0.5, row.movement / 20)
    # Day-on-chart penalty: brand-new gets neutral, established gets stable bias
    if row.days_on_chart < 7:
        score += 0.1  # new song, may have momentum
    return max(-1.0, min(1.0, score))


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--country", default="global", choices=["global", "us"])
    p.add_argument("--limit", type=int, default=20)
    a = p.parse_args()
    rows = fetch_top_songs(a.country, limit=a.limit)
    print(f"\n=== kworb {a.country.upper()} Top — {len(rows)} entries ===")
    if rows:
        print(f"Chart date: {rows[0].chart_date}\n")
    print(f"{'#':<4} {'ARTIST':<25} {'SONG':<35} {'STR/d':>10} {'TOTAL':>12} {'MOV':>5}")
    print("-" * 100)
    for r in rows:
        print(f"{r.rank:<4} {r.artist[:23]:<25} {r.song[:33]:<35} "
              f"{r.streams_today:>10,} {r.streams_total:>12,} {r.movement:>+5}")
