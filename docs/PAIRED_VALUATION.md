# Complementary inventory valuation

Replay now reports paired_hold_net_before_rewards_usd separately from the
existing immediate-liquidation net_before_rewards_usd. Matched YES and NO
quantities on the same contract are valued at their assumed combined $1 terminal
payoff. Only unmatched residual inventory is marked through observed exit depth
and charged the modeled exit fee. Entry fees apply to all fills.

This is terminal economic value, not cash, and it assumes the same-contract
complementary payoff remains applicable. Funding/time costs beyond the provided
operating-cost scenario are not modeled. No capital is released or recycled
before an actual offset/settlement receipt. Immediate liquidation remains an
explicit alternative, not an accounting error when that exit is actually chosen.

Three regressions cover paired economics, unmatched stale depth, and terminal
pair valuation without a fresh exit book. The change does not establish an edge,
implement active inventory management, or authorize live execution.
