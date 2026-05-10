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
