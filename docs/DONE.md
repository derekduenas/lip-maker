# 2026-05-10 LIP god-mode session — 4 phases, 4 commits

| Phase | Commit | Summary |
|---|---|---|
| 1 | `ec00824` | settlement_reconciler null-guard + 168h backfill — capture 27.5% → 58.6% MTD |
| 2 | `0d6dad3` | blocklist_review.py weekly cron with accrual fold-in — 0 unbans flagged |
| 3 | `01bba7a` | enrolment hygiene + `is_active_clause()` helper + write-time end_date gate — 46 stale → 0 |
| 4 | this   | `get_book_rank()` + lip_discovery filter (gated DISABLED) |

## Phase 4 calibration finding (2026-05-10)

`engine/depth_probe.get_book_rank()` shipped per spec, with 6 unit tests
passing. Wired into `top_n_to_quote()` behind `settings.DEPTH_PROBE_ENABLED`.

**Default is False.** Smoke test on 5 top-earning live markets:

| Ticker | target_size | yes pct | no pct | verdict |
|---|---|---|---|---|
| KXTRUMPPHOTO-26MAY16 | 300 | 369% | 238% | reject |
| KXMAMDANIEO-26MAY16-T0 | 300 | 809% | 747% | reject |
| KXCHAICUTS-26JUN04-T1 | 300 | 1616% | 771% | reject |
| KXCANHOUSTART-27JAN18-T275 | 300 | 2486% | 517% | reject |
| KXCANUSTRIPS-27FEB23-T20 | 300 | 1774% | 2318% | reject |

The spec's verdict thresholds (`<= 0.30 deploy / <= 0.50 marginal`) assume
percentile in [0, 1] — i.e., contracts_ahead bounded by target_size. Real
Kalshi orderbooks carry orders of magnitude more depth than LIP target.
Enabling the gate as specified would reject 100% of top earners and stop
the live engine.

**Follow-up before flipping ENABLED=True**:
- Calibrate thresholds against real percentile distribution (sample
  100+ active markets, fit thresholds to e.g. p50/p90 boundaries)
- Or change the metric: `our_size / (contracts_ahead + our_size)` matches
  the existing `filter_by_depth` pro-rata-share semantics
- Or normalize against per-market book depth instead of LIP target_size

## Tomorrow's queue (per session sign-off)

- Migrate 2 remaining callers (`macro_blackout_sync`, `news_kill`) to
  `is_active_clause()`
- Wire `depth_probe` re-probe loop into `run_paper.py` main loop
  (every 60s, watermark drift detection)
- Investigate KXSILVERMON +$3.88 (closest unban candidate)

---

# Phase 3 — Follow-up: end_date gating audit

After landing the source fix in `engine/lip_discovery.py:_decide_enrol`,
the helper `is_active_clause()`, the SQL cleanup, and the daily
`tools/lip_state_hygiene.py` cron, an audit of all callers that filter
on `enrolled = 1 AND paid_out = 0` showed:

## ALREADY GATED (multi-line WHERE includes `end_date > now`)
- `engine/capital_allocator.py:_enrolled_universe` — refactored to use
  `is_active_clause("p")` for single-source-of-truth (semantic no-op).
- `tools/competitor_density.py:170`
- `tools/anomaly_detector.py:211`
- `tools/price_velocity_kill.py:54`
- `tools/funnel_snapshot.py` (74, 80, 87, 384, 390, 397)
- `tools/pool_depletion.py:38`
- `engine/lip_discovery.py:290` (`top_n_to_quote`)

## STILL MISSING `end_date` GATE — follow-up
- `tools/macro_blackout_sync.py:43` — applies blackout markers to active
  markets. Hitting expired rows = redundant blackout writes, no trading
  impact.
- `tools/news_kill.py:110` — applies kill switch by news event prefix
  match. Hitting expired rows = pointless kill on already-closed markets.

Both are non-load-bearing (advisory writes, not trade gates). Fix is
trivially `f"WHERE {is_active_clause()} AND ..."`. Defer to a follow-up
commit when convenient — won't change observed behavior since
`tools/lip_state_hygiene.py` now demotes expired rows to `enrolled=0`
within 24h anyway.

## Earlier audit overcount
The original audit cited "~10 callers missing the gate." That was wrong
— grep on `WHERE enrolled = 1 AND paid_out = 0` matched the *first line*
of multi-line WHERE clauses; subsequent lines (including the `end_date`
filter) were not captured. Real number missing = 2.
