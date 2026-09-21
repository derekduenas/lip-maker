# Passive inventory pair completion

New research policy reward_inventory shares the reward-depth entry search,
then suppresses further purchases on the heavier side after a fill. It uses
FIFO unmatched lots with entry fees included. The opposite-side bid is capped
by 1 minus the highest unmatched entry cost, the new maker fee, and the explicit
minimum pair margin (default $.01). Quotes remain post-only and limited by
movement, spread, inventory and cash constraints. Activation rechecks imbalance
and caps hedge quantity to outstanding imbalance. No cash is released for paired
terminal value.

This is passive completion, not a guaranteed hedge or active stop-loss. Orders
may never fill and existing orders can fill during cancellation latency. It has
not been demonstrated profitable. It is included as an additional alternative
in program_experiment. Two synthetic regressions check pair completion and the
fee-inclusive price ceiling; 27 focused inventory/replay tests pass.
