# Contract reward economics — 2026-09-21

Implemented in research/reward_optimizer.py and exposed via:
`python -m tools.maker_research rank-rewards --input INPUT.json`.
Input keys are program (API incentive row), book (competitor-only normalized
book), candidates (explicit price, size, uptime and cost scenarios), now (epoch
seconds), horizon_seconds and tick_usd. See tests/test_reward_optimizer.py for a
complete working example. The horizon must fit the remaining program window.

## Sources and correction

Kalshi Help Center explains the reference level as cumulative TargetSize / 5,
with no distance penalty at or above reference. The older production scorer
incorrectly used the best bid; corrected and regression-tested.
https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program

The model applies side normalization, two-sided eligibility, excluded-time
scaling, cent rounding and the $1 minimum. It evaluates a fresh participation
plan; existing accrued rewards and overlapping program allocations are not yet
supported. It is not a settlement calculator. Constant share and qualified-time
forecasts are explicit assumptions. A full payout is known only after the period.

API specification retrieved 2026-09-21 identifies period_reward and
max_reward_per_account as centi-cents (divide by 10,000 for USD):
https://docs.kalshi.com/openapi.yaml

Event totals may combine multiple contract pools. Minimum payout is per program:
https://help.kalshi.com/en/articles/16076644-liquidity-and-volume-incentive-programs-where-to-find-them

## Decision output

For each alternative quote pair, recompute reference and cutoff AFTER adding
our liquidity; require competitor-only inputs to avoid double counting.
Return side cutoffs/reference, modeled share, estimated payable reward, capital,
scenario net dollars and net per capital-hour. Apply account cap before floor
and minimum. Reject inactive, stale, closed, mismatched and off-grid inputs.
Post-only crossing proposals cannot become research candidates.

Rank alternatives by net earnings per capital-hour. They are alternatives, not
independent additive investments. Capital includes full bid funding plus forecast
fees; no assumed collateral netting. Cost inputs must cover the exact horizon;
positive trade P&L is a caller forecast, not historical evidence. Missing cost
forecasts fail rather than silently defaulting to zero.

The output is SCENARIO_ESTIMATE_ONLY, live_eligible=false. Cross-market portfolio
allocation, calibrated fill/markout forecasts, settlement reconciliation and
independent validation remain necessary before live use. This module does not
change production routing. Governing regulatory notices remain authoritative;
the current Help Center was available but the notices listing did not expose
its underlying filing text during this check.
