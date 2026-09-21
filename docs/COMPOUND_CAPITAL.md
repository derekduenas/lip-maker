# Compounding controls

`python -m tools.maker_research compound-budget --input INPUT.json` replays
chronological receipts from a flat opening account. The input contains events,
mode, opening_cash, reserve_cash, deployment_fraction, and capacity_limit.
Defaults are a hypothetical $40 account, $5 reserve, 50% deployment of cash
above reserve, capped at $20. These are research controls, not an established
optimal allocation. The initial budget is $17.50, not $40.

Only cash from sales, settlements and paid reward credits can replenish funding.
Buy costs, fees and operating expenses reduce cash. Estimated rewards and unsold
inventory do not fund new orders. Duplicate identities do not compound twice;
conflicts, overspending, mixed modes and unsupported short positions fail.

Example: $40 plus $10 paid reward becomes $50 cash, with $20 deployment capacity
under the cap. An $800 pending reward changes neither cash nor deployment.
These examples are synthetic accounting checks, not achieved returns.

This command is deliberately an offline receipt replay: no scheduler or order
submission is introduced. It requires reconciled receipts and a flat starting
account. Outstanding order reservations and external transfers are unsupported;
its output is not exchange buying power and cannot authorize an order. Live
eligibility remains false. Profitability and market capacity must be measured
independently; the cap must not grow merely because a projection compounds well.
