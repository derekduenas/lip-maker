# Reward-depth program experiment

Command: `python -m tools.maker_research program-experiment --input INPUT.json`.
Input includes normalized events, exact program metadata, config and predeclared
sizes. Each size/policy is an alternative with its own budget, not simultaneous
capital deployment. The policies are do-nothing, join-best, and reward-depth.

Reward-depth evaluates 16 price pairs: best through three ticks deeper on each
side, recomputing the reference and cutoff with its proposed depth. It maximizes
current snapshot reward share per funded dollar within capital, inventory,
movement, spread and post-only constraints. Queue position and cancellation
latency remain modeled. Quote size is fixed per experiment, not chosen using
subsequent outcomes. The paper capture command now permits a $5,000 budget.

This is a reward-aware pricing hypothesis, not a net-profit optimizer: candidate
specific adverse-fill forecasts and active inventory hedging/unwinding are not
implemented. Inventory controls stop added risk; terminal liquidation uses the
observed book and scenario fees. Unknown lifecycle and incomplete program
coverage are explicit. Payout-if-stop does not imply that continued participation
could never reach the payment threshold. No estimates are compounded as cash.

The retained copper experiment uses previously inspected development data, two
sizes (10 and 50), capital $5,000, gross inventory cap 200 and net cap 100, 250ms
latency, queue multiplier 1, maker fee $.01 and exit fee $.02 per contract.
Those fees are assumptions. It is NOT a full-program or independent validation.
No strategy is selected/promoted. Neither the prior temporary signing credential
nor full-program recordings are configured here, so a fresh full-period run
remains unexecuted. This patch does not claim to complete that live experiment.
