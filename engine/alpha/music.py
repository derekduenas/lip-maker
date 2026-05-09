"""Music Alpha Pillar — directional edge on Kalshi music markets.

DATA: kworb.net (Spotify charts, daily streams, movement) — verified working.
HDD (album sales) coming as Phase 1.5 once Playwright integrated.

EDGE LOGIC (per agent research 2026-05-03):

  Markets we attack:
    - KXRANKLISTSONGSPOTGLOBAL/USA  → "Will [song] be #1 next Friday?"
    - KXTOPARTIST                    → "Who will be top artist?"

  Fair value model:
    Mid-week (Wed/Thu) we have ~60% of the week's daily streams in hand.
    For each market predicting "Will Song X be #1?":
      - Pull current week's daily totals from kworb
      - Linearly extrapolate to Thursday close
      - Compute prob = P(extrapolated > all challengers)
      - If divergence vs Kalshi > 5% → flag as edge

  Calibration:
    - Days 1-2 of week: low confidence (most data unknown)
    - Days 3-4 of week: medium
    - Days 5-7: HIGH (most stream weight already locked)

This is the SAME PATTERN Caleb Davies used to make $389k. He explicitly said
the daily-Spotify edge is now closed but stream-threshold + weekly-rank still
work for non-mega artists.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from engine.alpha.base import AlphaPillar
from engine.alpha.scrapers.kworb import (
    fetch_top_songs, normalize_for_match,
)

_log = logging.getLogger(__name__)


# Music series we have models for. New series added as we build coverage.
SUPPORTED_SERIES = {
    "KXRANKLISTSONGSPOTGLOBAL": {"chart": "global", "kind": "weekly_rank"},
    "KXRANKLISTSONGSPOTUSA":    {"chart": "us",     "kind": "weekly_rank"},
    "KXTOPARTIST":              {"chart": "global", "kind": "annual_artist"},
    "KXTOPARTISTUSA":           {"chart": "us",     "kind": "annual_artist"},
}

# Confidence based on day-of-week (chart resets Friday)
WEEKDAY_CONFIDENCE = {
    0: "low",     # Mon — fresh week
    1: "low",     # Tue
    2: "medium",  # Wed
    3: "medium",  # Thu
    4: "high",    # Fri (chart published)
    5: "high",    # Sat
    6: "high",    # Sun
}


class MusicAlpha(AlphaPillar):
    name = "music"
    min_edge_pct = 0.05         # 5% minimum to surface
    max_spread_cents = 30        # liquidity gate: skip wide-spread markets
    min_volume_24h = 5           # require some trading activity
    min_confidence_to_persist = "medium"   # never persist "low" confidence edges

    def fetch_external_data(self) -> dict:
        """Pull kworb global + US in one cycle. Cached upstream by HTTP."""
        global_top = fetch_top_songs("global", limit=200)
        us_top = fetch_top_songs("us", limit=200)
        if not global_top:
            _log.warning("kworb global empty — fair_prob disabled")
        return {
            "kworb_global": global_top,
            "kworb_us":     us_top,
            "fetched_at":   datetime.now(timezone.utc).isoformat(),
        }

    def discover_markets(self) -> list[dict]:
        """Find OPEN Kalshi music markets in our supported series."""
        from execution.kalshi_auth import KalshiClient
        c = KalshiClient()
        markets: list[dict] = []
        for series_ticker in SUPPORTED_SERIES:
            try:
                cursor = None
                while True:
                    params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    r = c.get_unauth("/markets", params=params)
                    batch = r.get("markets", []) or []
                    for m in batch:
                        # Compose dict the AlphaPillar pipeline expects
                        markets.append({
                            "ticker":         m.get("ticker", ""),
                            "series_ticker":  series_ticker,
                            "title":          m.get("title", "") or m.get("subtitle", ""),
                            "yes_bid":        float(m.get("yes_bid", 0)),
                            "yes_ask":        float(m.get("yes_ask", 100)),
                            "yes_price":      (float(m.get("yes_bid", 0)) +
                                               float(m.get("yes_ask", 100))) / 2,
                            "volume":         m.get("volume", 0),
                            "close_time":     m.get("close_time", ""),
                        })
                    cursor = r.get("cursor")
                    if not cursor:
                        break
            except Exception as e:
                _log.warning(f"discover {series_ticker} failed: {e}")
        return markets

    def compute_fair_prob(self, market: dict, external: dict) -> tuple[float, str]:
        """Estimate true probability that this market resolves YES.

        For weekly-rank markets like "Will [song] be #1 on Spotify Global?":
          - Match the song name in market title to kworb chart
          - If song is currently #1 with strong lead → high fair_prob
          - If song is #2-3 within 5% of #1 → medium fair_prob
          - Otherwise → low fair_prob
        """
        series = market["series_ticker"]
        spec = SUPPORTED_SERIES.get(series, {})
        chart_country = spec.get("chart", "global")
        chart = external.get(f"kworb_{chart_country}", [])
        if not chart:
            return (0.5, "low")

        title = market["title"].lower()
        # Try to identify which song/artist this market is about
        target_song, target_artist = _extract_song_from_title(market["title"])
        if not target_song and not target_artist:
            return (0.5, "low")

        # Find this artist's BEST-RANKED song in the chart
        matched_row = None
        artist_song_count = 0  # how many songs the artist has in top-200
        target_song_norm = normalize_for_match(target_song or "")
        target_artist_norm = normalize_for_match(target_artist or "")

        # If artist-only query (no specific song), find their highest-ranked song
        if target_artist_norm and not target_song_norm:
            for row in chart:
                if target_artist_norm in normalize_for_match(row.artist):
                    artist_song_count += 1
                    if matched_row is None or row.rank < matched_row.rank:
                        matched_row = row
        else:
            # Song-specific query
            for row in chart:
                if target_song_norm and target_song_norm in normalize_for_match(row.song):
                    if not target_artist_norm or target_artist_norm in normalize_for_match(row.artist):
                        matched_row = row
                        break

        if not matched_row:
            # Artist/song not in top-200 — extremely unlikely to be #1
            if "#1" in title or "number 1" in title.lower():
                return (0.02, "medium")
            return (0.10, "low")

        # Compute fair_prob based on position + momentum
        rank = matched_row.rank
        movement = matched_row.movement  # positive = climbing
        confidence = WEEKDAY_CONFIDENCE.get(datetime.now(timezone.utc).weekday(), "low")

        # If market asks "will X be #1":
        if rank == 1:
            # Currently #1, check lead size + momentum
            if len(chart) > 1:
                lead_pct = (matched_row.streams_today - chart[1].streams_today) / max(1, chart[1].streams_today)
                fair_prob = 0.65 + min(0.30, lead_pct * 2)  # bigger lead = higher prob
            else:
                fair_prob = 0.75
        elif rank == 2:
            fair_prob = 0.20 + (0.15 if movement > 0 else 0)  # climbing #2 has shot
        elif rank <= 5:
            fair_prob = 0.05 + (0.10 if movement > 5 else 0)
        else:
            fair_prob = 0.02
        # Normalize
        fair_prob = max(0.01, min(0.99, fair_prob))
        return (fair_prob, confidence)


def _extract_song_from_title(title: str) -> tuple[str, str]:
    """Parse Kalshi music market titles. Returns (song, artist).
    Common Kalshi formats:
      "Will [Artist] be #1 on the Any Spotify Daily Top Songs..."  → ("", artist)
      "Will [Song] by [Artist] reach..."                            → (song, artist)
      "[Song] by [Artist] - #1 on Spotify on [date]?"               → (song, artist)
    """
    # Pattern 1: "Will X be #1 ..."
    m = re.search(r"Will\s+(.+?)\s+be\s+#?\d", title, re.IGNORECASE)
    if m:
        artist = m.group(1).strip()
        # Strip "the" prefix and common qualifiers
        artist = re.sub(r"^the\s+", "", artist, flags=re.IGNORECASE)
        return ("", artist)
    # Pattern 2: "X by Y" anywhere
    m = re.search(r"\b([A-Z][\w\s'&.]+?)\s+by\s+([A-Z][\w\s'&.]+?)\b", title)
    if m:
        return (m.group(1).strip(), m.group(2).strip())
    # Pattern 3: quoted song
    m = re.search(r"['\"]([^'\"]{2,60})['\"]", title)
    if m:
        return (m.group(1), "")
    return ("", "")


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--bankroll", type=float, default=200.0)
    p.add_argument("--persist", action="store_true",
                   help="Save edges to alpha_predictions table")
    a = p.parse_args()
    pillar = MusicAlpha(bankroll_usd=a.bankroll)
    edges = pillar.find_edges()
    print(f"\n=== MUSIC ALPHA — {len(edges)} edges found ===\n")
    print(f"{'TICKER':<35} {'TITLE':<40} {'FAIR%':>6} {'MKT%':>6} {'EDGE':>7} {'CONF':>6} {'$REC':>6}")
    print("-" * 115)
    for e in edges[:25]:
        print(f"{e.market_ticker[:33]:<35} {e.features.get('market', '')[:38]:<40} "
              f"{e.fair_prob*100:>5.1f}% {e.market_yes_price*100:>5.1f}% "
              f"{e.edge_pct*100:>+6.2f}% {e.confidence:>6} ${e.recommended_usd:>5.2f}")
    if a.persist and edges:
        n = pillar.persist_predictions(edges)
        print(f"\n→ persisted {n} edges to alpha_predictions table")
