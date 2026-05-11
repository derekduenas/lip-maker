"""Step 0 — backfill historical_no1_rate_12mo per (artist, anchor_date).

Counts distinct months in the 12-month window STRICTLY BEFORE the
resolution month where any track crediting the artist hit #1 globally.
NO LEAKAGE: cutoff is anchor - 1 day; the resolution month itself is
excluded from the window.

Dependencies:
  - SpotifyChartsClient for per-track histories (already cached forever)
  - kworb artist pages to enumerate an artist's catalog
    (direct fetch via requests + cached identical to track pages)

Cache layout:
  data/cache/spotify/parsed/artist_{artist_id}_{fetch_date}.json
  data/cache/argus/historical_no1_{artist_slug}_{anchor}.json

Coverage caveat:
  We only check the artist's TOP_K tracks (lifetime streams sort), not
  their full catalog. Missing a fringe-track #1 hit is rare in practice
  but documented. For artists not findable on kworb, falls back to the
  observed YES base rate (training-set prior) — better than a zero spike
  the model would learn to ignore.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from argus.config import CACHE_DIR, DATA_DIR, ensure_dirs
from argus.data.spotify_charts import (
    SpotifyChartsClient, ChartEntry, _save_raw, _load_raw, USER_AGENT,
)

_log = logging.getLogger(__name__)

KWORB_BASE = "https://kworb.net/spotify"
TOP_K_TRACKS_PER_ARTIST = 15    # check top 15 tracks for any #1 in window
RATE_SLEEP_SEC = 1.0
_last_request_at = 0.0

# Fallback when artist isn't findable on kworb: training-set base rate.
# This is the observed YES rate from the settled-markets corpus. Set in
# runner.py when the backtest starts.
GLOBAL_YES_BASE_RATE = 0.116    # 8/69 from initial corpus


# ── HTTP (mirrors spotify_charts pattern) ─────────────────────────────────
def _http_get(url: str) -> str:
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < RATE_SLEEP_SEC:
        time.sleep(RATE_SLEEP_SEC - elapsed)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
        return r.text
    finally:
        _last_request_at = time.time()


# ── Artist-ID resolution ──────────────────────────────────────────────────
def _artist_id_map_path() -> Path:
    ensure_dirs()
    p = CACHE_DIR / "spotify" / "artist_id_map.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_artist_id_map() -> dict[str, str]:
    p = _artist_id_map_path()
    if not p.exists():
        return {}
    try:
        return json.load(open(p))
    except Exception:
        return {}


def save_artist_id_map(m: dict[str, str]) -> None:
    json.dump(m, open(_artist_id_map_path(), "w"), indent=2, sort_keys=True)


def update_artist_id_map_from_chart(chart: list[ChartEntry]) -> dict[str, str]:
    """Side-effect: adds any new (artist_name -> kworb_id) pairs found in a chart.
    Returns the updated map.

    NOTE: ChartEntry doesn't currently store the artist_id (the kworb URL).
    We re-derive by fetching the chart's raw HTML — for Phase 4 simplicity
    we re-scrape; in V2 the SpotifyChartsClient should expose artist_id."""
    return load_artist_id_map()    # placeholder — see find_artist_id below


_RX_ARTIST_LINK = re.compile(r'href="\.\./artist/([A-Za-z0-9]+)\.html">([^<]+)</a>')


def find_artist_id(artist_name: str, force_refresh: bool = False) -> Optional[str]:
    """Resolve artist_name → kworb_artist_id. Caches result.

    Strategy:
      1. Check cached map.
      2. Fetch the latest global chart raw HTML, scan all artist links.
      3. Persist any new mappings discovered.
    """
    name_canon = (artist_name or "").strip()
    if not name_canon:
        return None
    m = load_artist_id_map()
    if name_canon in m and not force_refresh:
        return m[name_canon]

    # Re-derive from chart HTML (cached identically to spotify_charts)
    today = dt.date.today()
    raw_p = CACHE_DIR / "spotify" / "raw" / f"chart_global_{today.isoformat()}.html.gz"
    if raw_p.exists():
        html = _load_raw(raw_p) or ""
    else:
        html = _http_get(f"{KWORB_BASE}/country/global_daily.html")
        raw_p.parent.mkdir(parents=True, exist_ok=True)
        _save_raw(raw_p, html)

    found = 0
    for aid, name in _RX_ARTIST_LINK.findall(html):
        if name not in m:
            m[name] = aid
            found += 1
    if found:
        save_artist_id_map(m)
        _log.info(f"artist_id_map: +{found} new entries → {len(m)} total")
    return m.get(name_canon)


# ── Artist page scraper ───────────────────────────────────────────────────
def _artist_page_cache(artist_id: str, fetch_date: dt.date) -> Path:
    return CACHE_DIR / "spotify" / "raw" / f"artist_{artist_id}_{fetch_date.isoformat()}.html.gz"


def get_artist_top_tracks(
    artist_id: str,
    top_k: int = TOP_K_TRACKS_PER_ARTIST,
    on_date: Optional[dt.date] = None,
) -> list[tuple[str, str]]:
    """Return list of (track_id, name) for an artist's top tracks (kworb order)."""
    on_date = on_date or dt.date.today()
    p = _artist_page_cache(artist_id, on_date)
    if p.exists():
        html = _load_raw(p) or ""
    else:
        html = _http_get(f"{KWORB_BASE}/artist/{artist_id}.html")
        p.parent.mkdir(parents=True, exist_ok=True)
        _save_raw(p, html)
    # Track links on artist page
    links = re.findall(r'<a href="\.\./track/([A-Za-z0-9]+)\.html">([^<]+)</a>', html)
    seen, uniq = set(), []
    for tid, name in links:
        if tid in seen:
            continue
        seen.add(tid)
        uniq.append((tid, name))
        if len(uniq) >= top_k:
            break
    return uniq


# ── Backfill ──────────────────────────────────────────────────────────────
@dataclass
class HistoricalNo1:
    artist_name:        str
    anchor_date:        dt.date         # first day of resolution month
    lookback_start:     dt.date
    lookback_end:       dt.date         # = anchor - 1 day
    months_with_no1:    int
    rate:               float           # months_with_no1 / 12
    tracks_examined:    int
    tracks_with_no1:    list[str]       # track_ids that hit #1 in window
    method:             str             # "kworb" | "fallback_base_rate"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)[:60]


def _backfill_cache_path(artist_name: str, anchor: dt.date) -> Path:
    p = CACHE_DIR / "argus" / "historical_no1"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{_slug(artist_name)}_{anchor.isoformat()}.json"


def compute_historical_no1_rate(
    artist_name:    str,
    anchor_date:    dt.date,
    client:         SpotifyChartsClient,
    base_rate_fallback: float = GLOBAL_YES_BASE_RATE,
) -> HistoricalNo1:
    """Compute fraction of past 12 months where artist had any #1 song.

    Window: [anchor - 365 days, anchor - 1 day]. Resolution month strictly
    excluded. Uses cached track histories where available.
    """
    cache_p = _backfill_cache_path(artist_name, anchor_date)
    if cache_p.exists():
        try:
            d = json.load(open(cache_p))
            return HistoricalNo1(
                artist_name=d["artist_name"],
                anchor_date=dt.date.fromisoformat(d["anchor_date"]),
                lookback_start=dt.date.fromisoformat(d["lookback_start"]),
                lookback_end=dt.date.fromisoformat(d["lookback_end"]),
                months_with_no1=d["months_with_no1"],
                rate=d["rate"],
                tracks_examined=d["tracks_examined"],
                tracks_with_no1=d["tracks_with_no1"],
                method=d["method"],
            )
        except Exception:
            pass

    lookback_start = anchor_date - dt.timedelta(days=365)
    lookback_end   = anchor_date - dt.timedelta(days=1)

    artist_id = find_artist_id(artist_name)
    if not artist_id:
        h = HistoricalNo1(
            artist_name=artist_name, anchor_date=anchor_date,
            lookback_start=lookback_start, lookback_end=lookback_end,
            months_with_no1=int(round(base_rate_fallback * 12)),
            rate=base_rate_fallback, tracks_examined=0, tracks_with_no1=[],
            method="fallback_base_rate",
        )
        _persist(cache_p, h)
        return h

    tracks = get_artist_top_tracks(artist_id)
    months_with_no1: set[tuple[int, int]] = set()
    tracks_with_hit: list[str] = []
    for tid, _name in tracks:
        try:
            th = client.get_track_history(tid)
        except Exception as e:
            _log.warning(f"track {tid} fetch failed: {e}")
            continue
        for w in th.weeks:
            if w.country != "GLOBAL":
                continue
            # Strict cutoff — week_start must be in [lookback_start, lookback_end]
            if not (lookback_start <= w.week_start <= lookback_end):
                continue
            if w.rank == 1:
                months_with_no1.add((w.week_start.year, w.week_start.month))
                if tid not in tracks_with_hit:
                    tracks_with_hit.append(tid)
                break    # one #1 in window is enough for THIS track

    rate = len(months_with_no1) / 12.0
    h = HistoricalNo1(
        artist_name=artist_name, anchor_date=anchor_date,
        lookback_start=lookback_start, lookback_end=lookback_end,
        months_with_no1=len(months_with_no1),
        rate=rate, tracks_examined=len(tracks),
        tracks_with_no1=tracks_with_hit, method="kworb",
    )
    _persist(cache_p, h)
    return h


def _persist(path: Path, h: HistoricalNo1) -> None:
    json.dump({
        "artist_name":     h.artist_name,
        "anchor_date":     h.anchor_date.isoformat(),
        "lookback_start":  h.lookback_start.isoformat(),
        "lookback_end":    h.lookback_end.isoformat(),
        "months_with_no1": h.months_with_no1,
        "rate":            h.rate,
        "tracks_examined": h.tracks_examined,
        "tracks_with_no1": h.tracks_with_no1,
        "method":          h.method,
    }, open(path, "w"), indent=2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    c = SpotifyChartsClient()

    # Spot-check: Bieber anchor 2026-05-01 (should find recent activity)
    anchor = dt.date(2026, 5, 1)
    for name in ["Justin Bieber", "Olivia Rodrigo", "Madonna", "Nicki Minaj"]:
        h = compute_historical_no1_rate(name, anchor, c)
        print(f"{name:<20} rate={h.rate:.3f}  months={h.months_with_no1}/12  "
              f"tracks={h.tracks_examined}  hits={len(h.tracks_with_no1)}  method={h.method}")
    print("OK historical_no1")
