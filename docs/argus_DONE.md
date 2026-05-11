# ARGUS — Project DONE log

## 2026-05-10 → 2026-05-11 — Phase 1-5 build

| Phase | Commit | Summary |
|---|---|---|
| 1 | `d9d67d7` | architectural skeleton + base classes (21 files, 17 tests) |
| 2 | `46d562b` | Spotify Charts data layer (kworb scraper) |
| 3 | `058165c` | MusicBrain v1 + BSS-aware gate metric (replaces raw Brier) |
| 4 | `cb64bde` | backtest + calibration — **BSS = +0.43 on n=46, gate ≥ 0.15 PASS** |
| 5 | this   | persisted weights + paper-mode scanner + cron |

## Final Phase 5 numbers

**Trained model** (`data/argus/music_model_v1.json`):
- version: v1
- trained_at: 2026-05-11T01:51:19Z
- n_training_examples: 46 (full corpus retrain)
- base_rate: 0.174
- test_metrics: BSS=0.430, Brier=0.082, naive_brier=0.144, n_test=46

**Live scan** (2026-05-11T02:20Z):
- 40 active KXRANKLISTSONGSPOTGLOBAL-* markets pulled
- 40 predictions emitted (100% — all artists found on kworb)
- 11 actionable (edge ≥ 8pp + confidence ≥ 0.5)

Top candidate (negative edge — biggest disagreement):
```
KXRANKLISTSONGSPOTGLOBAL-26JUN01-ARI  side=NO  edge=-59.94pp
  our_p=0.14  market_p=0.73  confidence=0.53
  current_top_track_rank=199  days_factor=0.68  historical_no1_rate=0.00
```
Most other top candidates: long-tail artists where market trades 1-4% but
model assigns the bare prior ~14%; small YES bets in low-prob region.

## Per-prong context (for posterity)

| Prong | Type | Status as of 2026-05-11 |
|---|---|---|
| 1 LIP | rebate physics | live, generating cash; ~$130/mo MTD |
| 2 Sovereign | linguistic predictor | Brier 0.176 GREEN, paper-only, NVDA+WMT 5/20-21 next |
| 3 Dislocation | cross-venue convergence | parity gate passing on next-meeting subset, awaiting Kalshi market listings |
| 4 ARGUS Music | directional alpha | **BSS 0.43, scanner live in paper mode** |

## Phase 5 acceptance — all met

- ✓ `data/argus/music_model_v1.json` saved + reloadable (legacy + v1 schemas both supported by `MusicModel.load`)
- ✓ `tools/argus_scan.py` runs end-to-end against live Kalshi
- ✓ 11 actionable candidates surfaced (≥ 5 acceptance bar)
- ✓ Top candidate's feature breakdown shows sensible drivers
- ✓ `deploy/argus-scan.{timer,service}` committed for daily 23:00 UTC
- ✓ Velocity-feature live-vs-backtest monitor wired: every prediction
  (actionable or not) persists `feature_audit_json` to `argus_candidates`
  for future analysis cron

## Velocity-feature monitor — analysis recipe

After ~30 days of scans, run:
```sql
SELECT
  CASE
    WHEN json_extract(feature_audit_json, '$.streams_velocity_norm_today') > 0.5
      THEN 'high_pos_vel'
    WHEN json_extract(feature_audit_json, '$.streams_velocity_norm_today') < -0.5
      THEN 'high_neg_vel'
    ELSE 'mid'
  END AS vel_bucket,
  AVG(our_p) AS avg_pred,
  COUNT(*)   AS n
FROM argus_candidates
GROUP BY vel_bucket;
```
Then cross-reference predictions against settled outcomes (joining to a
`settled_markets` snapshot). If high-positive-velocity predictions hit YES
more often than the model expected, the negative backtest weight is
artifact (week-over-week proxy ≠ daily delta) and v2 should retrain.

## NOT shipped (intentionally deferred)

- LIVE_MODE auto-execution. Paper-only this session.
- v2 with calibrated probabilities (Platt scaler skeleton exists in
  `argus/scoring/calibration.py`; deploy after live data validates).
- Brain #2 (Weather, NWS forecasts ↔ KXHIGHT*). Same architecture, fresh
  data adapter. Likely the next ROI/effort target.
- Cron installation on prod. Operator manually:
  ```
  cp deploy/argus-scan.{timer,service} /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now argus-scan.timer
  ```

## Operator notes

- Cache locations:
  - `data/cache/spotify/raw/` — gzipped HTML, immutable, never expire
  - `data/cache/spotify/parsed/` — JSON dataclasses
  - `data/cache/argus/historical_no1/` — per (artist, anchor) backfills
- All caches are content-addressable; safe to wipe `parsed/` (re-parse from
  raw) but NEVER wipe `raw/` without re-scraping.
- Branch `claude/argus-foundation` is ready for review + merge to main.
