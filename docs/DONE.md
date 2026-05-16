# 2026-05-10 (later): Sovereign + Dislocation wake

## Sovereign Engine A wake-up

Engine A (`loop/monitor.py` via `sovereign.service`) was DISABLED + never
been active. Only Engine B (FOMC `prep/scheduler.py`) was running, correctly
reporting "no milestones due" because next FOMC is 2026-06-17.

**Architecture clarification**: events live in `config/events.py:UPCOMING_EVENTS`
(Python list), NOT in `sovereign_live_events` DB table (which is for FOMC live
ticks). `loop/monitor.py` polls the Python list each cycle.

Wired:
- HIMS earnings 2026-05-11 (Kalshi-verified, 14 markets active under
  `KXEARNINGSMENTIONHIMS-26MAY11-*`). Speaker key `hims` → corpus n=0
  → thin-corpus REJECT verified live (`monitor.py --once` returned 0
  qualifying opportunities on 14 markets).
- NVDA 2026-05-21 (huang corpus n=8 ✓; date corrected from 5/28 per spec)
- WMT  2026-05-21 (mcmillon corpus n=8 ✓)
- `sovereign.service` ENABLED + ACTIVE since 2026-05-10 21:39 UTC

Side effect: 5 stale April paper trades resolved (4L/1W, -$7.84).

## Dislocation parity gate — far-future math deferred

Far-future contracts (Jul 2027+) require multi-meeting tree pricing not
yet implemented. Including those rows fails the parity gate via expected
model gaps (94 rows, 51% within 1pp, mean 9.47pp), but next-meeting subset
passes cleanly (2 rows, 100% within 1pp, mean 0.84pp).

Path-dependent multi-meeting math is a **deferred feature, not a bug**.
`evaluate_gate()` now defaults to `next_meeting_only=True` and ParityResult
carries `is_next_meeting`. `compute_parity()` infers it as "earliest
fomc_date for the snapshot's snapshot_date".

Cherry-picked from `claude/optimize-trading-system-MszGM`:
- `d4f73e6` Prong 3 dislocation module
- `4a249cd` backtest harness
- `0c4c992` parity scaffolding
- `b3cc13d` Gaussian-smoothed decision_probs

Smoke: `tools.dislocation_scan --domain macro_fed` importable on prod;
0 actionable Fed candidates (most `KXFEDDECISION-26{JUN,JUL,SEP,NOV}-*`
return 404 — series lifecycle not yet published).

---

# 2026-05-10 LIP god-mode session

| Phase | Commit | Summary |
|---|---|---|
| 1 | `ec00824` | settlement_reconciler null-guard + 168h backfill — capture 27.5% → 58.6% MTD |
| 2 | `0d6dad3` | blocklist_review.py weekly cron with accrual fold-in — 0 unbans flagged |
| 3 | `01bba7a` | enrolment hygiene + `is_active_clause()` helper — 46 stale → 0 |
| 4 | `b47c4fb` → reverted | depth_probe `get_book_rank()` — duplicate logic |

## Phase 4: REVERTED

`filter_by_depth()` already implemented projected_share with `min_share=0.05`
at `run_paper.py:977,1044`. Phase 4's `get_book_rank()` was duplicate logic
with the wrong metric (`contracts_ahead / target_size` instead of pro-rata
share). Reverted, no functional change.

**Audit takeaway**: `lip_discovery` filter layer not needed; `run_paper.py`
is the right enforcement point — closer to the actual deploy decision.

**Follow-up for tomorrow** (do NOT ship without scope confirmation):
Consider enhancing `filter_by_depth()` with:
- "marginal" tier (1–5% share) + structured logging
- Verbose return dict for observability
- Configurable thresholds in `config/settings.py`

These would be cosmetic/diagnostic improvements on top of the existing
correct logic, not a new filter.

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
