# ARGUS — Spotify Charts Data Source Comparison

Date: 2026-05-10
Decision: **kworb.net** as primary source for Music Brain v1.

## Candidates evaluated

| Source | Auth | Daily chart? | Per-track history? | Stream counts? | Cost | Coverage |
|---|---|---|---|---|---|---|
| **kworb.net** | none (scrape) | ✓ Top 200 daily, per country | ✓ Weekly, since 2024/08/22 | ✓ Yes | $0 | Global + 50+ countries |
| Spotify Web API | OAuth | ✗ no chart endpoint | ✗ only `popularity` 0-100 | ✗ never | $0 (rate limited) | One-track lookup only |
| Apple Music RSS | none | ✓ Top 100 US daily | ✗ no per-song endpoint | ✗ ranks only | $0 | Apple population (different from Spotify) |
| Last.fm | API key (free) | partial (Last.fm charts ≠ Spotify) | ✓ via user.getRecentTracks | ✗ scrobbles, not streams | $0 | Last.fm user base only |
| Songstats free tier | account required | unknown (waitlist) | unknown | unknown | $0 → paid | unknown |
| Chartmetric | account (waitlisted) | ✓ comprehensive | ✓ rich history | ✓ | paid mostly | comprehensive |

## Live probe results (2026-05-10)

```
GET kworb.net/spotify/country/global_daily.html
  HTTP 200, 88KB, 1.1s — 200 chart rows with rank/move/weeks/peak/
  streams_today/delta/streams_7d/delta/total_streams + artist + track ID

GET kworb.net/spotify/track/{TRACK_ID}.html
  HTTP 200, 459KB — two tables:
    [0] 44 rows = weekly per-country trajectory since 2024/08/22
    [1] 34 rows = per-country totals/peaks

GET rss.applemarketingtools.com/api/v2/us/music/most-played/100/songs.json
  HTTP 200, 65KB, 100 songs — name + artist only, no streams, no history

GET archive.org/wayback (kworb.net snapshots)
  archived_snapshots: {} — Wayback has zero kworb history.
  Implication: cache forward starting Day 1; we own our archive.
```

## Why kworb wins

1. **Only free source with all three signals**: rank, daily streams, per-track history.
2. **Weekly granularity sufficient for backtest**: Kalshi `KXRANKLISTSONGSPOT*` markets settle weekly; we don't need finer than that for the held-out test.
3. **Per-country dimensionality** unlocks future brains for country-specific contracts.
4. **No API key, no rate limits documented** — robotic scraping at moderate cadence (4× daily) should remain in good standing.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| HTML parser breaks on layout change | Regex is loose; cache fail-open; alert on schema mismatch |
| kworb goes down or goes paid | Add Apple Music + Spotify-API-popularity as cross-references; keep cache forever |
| Wayback has no archive → can't backfill before today | Start crawl now; in 30 days we have a 30-day archive. Backtest defers until enough history. |
| Coverage gap (track not on chart any given week) | predict() returns None for unseen tracks; gate handles missing-corpus rejection |

## What we're not building (yet)

- **Cross-source ensembling** — Apple/Last.fm as cross-checks for Music Brain v2.
- **Spotify Web API integration** — only useful if we want metadata (release date, genre) for feature engineering. Defer until v2.
- **Songstats / Chartmetric** — paid tier likely justified once Music Brain proves ROI. Defer.

## Source-of-truth note

Every fetched chart and track page goes to `data/cache/spotify/`, indexed by
`(kind, key, date)` and never expires. Cache keys are content-addressable so
we can survive HTML layout changes by re-parsing from the raw cached payload.
