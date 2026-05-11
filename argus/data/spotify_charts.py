"""Spotify charts adapter — kworb.net scraper.

Why kworb (see docs/argus_spotify_source_comparison.md): only free source
that provides daily chart positions + per-track stream history + per-country
breakdowns. Spotify Web API has no chart endpoint and no history. Apple
Music RSS has chart but no streams. Last.fm uses scrobble population.

Data shape (kworb):
    Daily chart page    /spotify/country/{country}_daily.html
        200 rows: rank | move | "Artist - Title" + track_id
                  weeks | peak | (xN) | streams_today | delta | streams_7d | delta | total
    Per-track page      /spotify/track/{track_id}.html
        Weekly trajectory back to song debut (since 2024/08/22 minimum)
        Per-country rank + streams per week

Cache strategy:
    Every fetched HTML payload is persisted to data/cache/spotify/raw/
    indexed by (kind, key, fetch_date). Parsed JSON also cached. Cache
    NEVER expires — historical chart data is immutable. Re-parse from raw
    if the HTML schema changes; only refetch on operator force.

Polite scraping:
    - User-Agent identifies us as a scientific scraper
    - 1.0s between requests (well under any reasonable rate limit)
    - Single fetch per unique (country, date) ever
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import requests

from argus.config import CACHE_DIR, ensure_dirs

_log = logging.getLogger(__name__)

USER_AGENT = "ArgusBot/0.1 (research; +https://github.com/derekduenas/lip-maker)"
KWORB_BASE = "https://kworb.net/spotify"
RATE_SLEEP_SEC = 1.0

# Country code → kworb URL slug
COUNTRY_SLUGS = {"global": "global", "us": "us", "gb": "gb", "ca": "ca",
                 "au": "au", "fr": "fr", "de": "de", "br": "br", "mx": "mx"}


# ── Data classes ──────────────────────────────────────────────────────────
@dataclass
class ChartEntry:
    rank:               int
    move:               str           # "=" / "+1" / "-2" / "NEW" / "RE"
    artist:             str
    track_name:         str
    track_id:           Optional[str]
    weeks_on_chart:     int
    peak_position:      int
    streams_today:      int
    streams_today_delta:int
    streams_7d:         int
    streams_7d_delta:   int
    total_streams:      int


@dataclass
class TrackChartWeek:
    """One week's data point on a track's per-country chart history."""
    week_start:    dt.date    # date kworb labels this week with (Thursday)
    country:       str
    rank:          int
    streams:       int


@dataclass
class TrackHistory:
    track_id:        str
    artist:          str
    track_name:      str
    weeks:           list[TrackChartWeek] = field(default_factory=list)

    def for_country(self, country: str) -> list[TrackChartWeek]:
        c = country.upper()
        return [w for w in self.weeks if w.country == c]


# ── Cache helpers ─────────────────────────────────────────────────────────
def _cache_root() -> Path:
    ensure_dirs()
    root = CACHE_DIR / "spotify"
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "parsed").mkdir(parents=True, exist_ok=True)
    return root


def _raw_path(kind: str, key: str, fetch_date: dt.date) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
    return _cache_root() / "raw" / f"{kind}_{safe}_{fetch_date.isoformat()}.html.gz"


def _parsed_path(kind: str, key: str, fetch_date: dt.date) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
    return _cache_root() / "parsed" / f"{kind}_{safe}_{fetch_date.isoformat()}.json"


def _save_raw(path: Path, html: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(html)


def _load_raw(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read()


# ── HTTP ──────────────────────────────────────────────────────────────────
_last_request_at = 0.0


def _http_get(url: str) -> str:
    """GET with rate limit + identifying user agent. Raises on non-200."""
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < RATE_SLEEP_SEC:
        time.sleep(RATE_SLEEP_SEC - elapsed)
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.HTTPError as e:
        raise RuntimeError(f"HTTP {e.response.status_code} on {url}") from e
    finally:
        _last_request_at = time.time()


# ── Parsers ───────────────────────────────────────────────────────────────
_RX_TRACK_ID  = re.compile(r"href=\"\.\./track/([A-Za-z0-9]+)\.html\"")
_RX_TRACK_TXT = re.compile(r">([^<]+)</a>")  # naive textual extract
_RX_INT_COMMA = re.compile(r"-?[\d,]+")


def _to_int(s: str) -> int:
    s = s.replace(",", "").replace("+", "").strip()
    if not s or s == "-":
        return 0
    try:
        return int(s)
    except ValueError:
        return 0


def parse_chart_html(html: str) -> list[ChartEntry]:
    """Parse a kworb daily chart page → list of ChartEntry."""
    out: list[ChartEntry] = []
    m_body = re.search(r"<tbody.*?>(.*?)</tbody>", html, re.S)
    if not m_body:
        return out
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m_body.group(1), re.S)
    for raw in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", raw, re.S)
        if len(cells) < 11:
            continue
        try:
            rank  = _to_int(re.sub(r"<[^>]+>", "", cells[0]))
            move  = re.sub(r"<[^>]+>", "", cells[1]).strip()
            text_cell = cells[2]
            tid_m = _RX_TRACK_ID.search(text_cell)
            track_id = tid_m.group(1) if tid_m else None
            # Strip <a> tags but keep their text; split on " - "
            stripped = re.sub(r"<[^>]+>", "", text_cell).strip()
            if " - " in stripped:
                artist, _, track_name = stripped.partition(" - ")
            else:
                artist, track_name = "", stripped
            weeks  = _to_int(re.sub(r"<[^>]+>", "", cells[3]))
            peak   = _to_int(re.sub(r"<[^>]+>", "", cells[4]))
            # cells[5] is "(xN)" — skip
            streams_today       = _to_int(re.sub(r"<[^>]+>", "", cells[6]))
            streams_today_delta = _to_int(re.sub(r"<[^>]+>", "", cells[7]))
            streams_7d          = _to_int(re.sub(r"<[^>]+>", "", cells[8]))
            streams_7d_delta    = _to_int(re.sub(r"<[^>]+>", "", cells[9]))
            total_streams       = _to_int(re.sub(r"<[^>]+>", "", cells[10]))
        except Exception as e:
            _log.debug(f"row parse failed: {e}")
            continue
        out.append(ChartEntry(
            rank=rank, move=move, artist=artist.strip(), track_name=track_name.strip(),
            track_id=track_id, weeks_on_chart=weeks, peak_position=peak,
            streams_today=streams_today, streams_today_delta=streams_today_delta,
            streams_7d=streams_7d, streams_7d_delta=streams_7d_delta,
            total_streams=total_streams,
        ))
    return out


def parse_track_html(html: str, track_id: str) -> TrackHistory:
    """Parse a kworb per-track page → TrackHistory (weekly per-country)."""
    title_m = re.search(r"<title>([^<]+) Spotify Chart History</title>", html)
    artist, track_name = "", ""
    if title_m:
        full = title_m.group(1).strip()
        if " - " in full:
            artist, _, track_name = full.partition(" - ")
            track_name = track_name.strip()
            artist = artist.strip()
    weeks: list[TrackChartWeek] = []

    # First table is the chart-history grid: rows are Date | Global | US | ...
    m = re.search(r"<table[^>]*>(.*?)</table>", html, re.S)
    if not m:
        return TrackHistory(track_id=track_id, artist=artist, track_name=track_name)
    table = m.group(1)

    # Header row determines country order (skip "Date" col)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
    if not rows:
        return TrackHistory(track_id=track_id, artist=artist, track_name=track_name)

    header_cells = re.findall(r"<th[^>]*>([^<]+)</th>", rows[0])
    # header_cells = ['Date', 'Global', 'US', 'ID', ...]
    if not header_cells or header_cells[0].strip() != "Date":
        return TrackHistory(track_id=track_id, artist=artist, track_name=track_name)
    countries = [c.strip().upper() for c in header_cells[1:]]

    for raw in rows[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", raw, re.S)
        if not cells or len(cells) - 1 < len(countries):
            continue
        date_text = re.sub(r"<[^>]+>", "", cells[0]).strip()
        # Skip Total / Peak rows; only YYYY/MM/DD entries
        m_date = re.match(r"(\d{4})/(\d{2})/(\d{2})", date_text)
        if not m_date:
            continue
        try:
            week_start = dt.date(int(m_date.group(1)), int(m_date.group(2)),
                                 int(m_date.group(3)))
        except ValueError:
            continue
        for i, country in enumerate(countries):
            cell = cells[i + 1]
            if "<span" not in cell:
                continue
            # <span class="p">RANK</span><span class="b"> (</span><span class="s">STREAMS</span>
            r_m = re.search(r'<span class="p">(\d+)</span>', cell)
            s_m = re.search(r'<span class="s">([\d,]+)</span>', cell)
            if not r_m:
                continue
            weeks.append(TrackChartWeek(
                week_start=week_start, country=country,
                rank=int(r_m.group(1)),
                streams=_to_int(s_m.group(1)) if s_m else 0,
            ))
    return TrackHistory(track_id=track_id, artist=artist, track_name=track_name, weeks=weeks)


# ── Public client ─────────────────────────────────────────────────────────
class SpotifyChartsClient:
    """Cache-first kworb client.

    Cache hit: deserialize parsed JSON.
    Cache miss: HTTP fetch → save raw HTML → parse → save parsed JSON.
    """
    def __init__(self, *, force_refresh: bool = False) -> None:
        self.force_refresh = force_refresh

    def get_chart(self, country: str = "global",
                  on_date: Optional[dt.date] = None) -> list[ChartEntry]:
        """Fetch the daily Top 200 for `country`. on_date is the fetch date
        (cache key). If cached, returns cached entries. kworb only serves
        the 'today' chart — older fetches are cached forever."""
        on_date = on_date or dt.date.today()
        slug = COUNTRY_SLUGS.get(country.lower(), country.lower())
        kind = "chart"
        key  = country.lower()
        parsed_p = _parsed_path(kind, key, on_date)

        if parsed_p.exists() and not self.force_refresh:
            try:
                with open(parsed_p) as f:
                    return [ChartEntry(**d) for d in json.load(f)]
            except Exception as e:
                _log.warning(f"parsed cache corrupted ({parsed_p.name}): {e} — refetch")

        url = f"{KWORB_BASE}/country/{slug}_daily.html"
        raw_p = _raw_path(kind, key, on_date)
        if raw_p.exists() and not self.force_refresh:
            html = _load_raw(raw_p) or ""
        else:
            _log.info(f"fetch {url}")
            html = _http_get(url)
            _save_raw(raw_p, html)

        entries = parse_chart_html(html)
        with open(parsed_p, "w") as f:
            json.dump([asdict(e) for e in entries], f)
        return entries

    def get_track_history(self, track_id: str,
                          on_date: Optional[dt.date] = None) -> TrackHistory:
        """Fetch a track's full per-country weekly trajectory."""
        on_date = on_date or dt.date.today()
        kind = "track"
        key  = track_id
        parsed_p = _parsed_path(kind, key, on_date)

        if parsed_p.exists() and not self.force_refresh:
            try:
                with open(parsed_p) as f:
                    d = json.load(f)
                weeks = [TrackChartWeek(
                    week_start=dt.date.fromisoformat(w["week_start"]),
                    country=w["country"], rank=w["rank"], streams=w["streams"],
                ) for w in d["weeks"]]
                return TrackHistory(
                    track_id=d["track_id"], artist=d["artist"],
                    track_name=d["track_name"], weeks=weeks,
                )
            except Exception as e:
                _log.warning(f"parsed cache corrupted ({parsed_p.name}): {e} — refetch")

        url = f"{KWORB_BASE}/track/{track_id}.html"
        raw_p = _raw_path(kind, key, on_date)
        if raw_p.exists() and not self.force_refresh:
            html = _load_raw(raw_p) or ""
        else:
            _log.info(f"fetch {url}")
            html = _http_get(url)
            _save_raw(raw_p, html)

        h = parse_track_html(html, track_id)
        with open(parsed_p, "w") as f:
            json.dump({
                "track_id":   h.track_id,
                "artist":     h.artist,
                "track_name": h.track_name,
                "weeks":      [{"week_start": w.week_start.isoformat(),
                                "country": w.country, "rank": w.rank,
                                "streams": w.streams} for w in h.weeks],
            }, f)
        return h


# ── Self-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    c = SpotifyChartsClient()

    print("=== Top 5 (global) ===")
    chart = c.get_chart("global")
    print(f"fetched {len(chart)} entries")
    for e in chart[:5]:
        print(f"  #{e.rank:>3} {e.move:<3} {e.artist[:25]:<25} - {e.track_name[:30]:<30}  "
              f"streams_today={e.streams_today:>10,}  weeks={e.weeks_on_chart}")

    if chart:
        sample = chart[0]
        if sample.track_id:
            print(f"\n=== Track history: {sample.artist} - {sample.track_name} ({sample.track_id}) ===")
            th = c.get_track_history(sample.track_id)
            print(f"  artist:      {th.artist}")
            print(f"  track:       {th.track_name}")
            print(f"  total weeks: {len(th.weeks)}")
            global_weeks = th.for_country("Global")
            global_weeks.sort(key=lambda w: w.week_start, reverse=True)
            print(f"  Global weeks: {len(global_weeks)}")
            for w in global_weeks[:5]:
                print(f"    {w.week_start.isoformat()}  rank={w.rank:<4}  streams={w.streams:,}")

    # Cache hit test
    print("\n=== Cache hit verify ===")
    t0 = time.time()
    chart2 = c.get_chart("global")
    elapsed_ms = (time.time() - t0) * 1000
    print(f"  2nd call: {len(chart2)} entries in {elapsed_ms:.1f}ms (should be <100ms)")
    assert len(chart2) == len(chart)
    print("OK spotify_charts")
