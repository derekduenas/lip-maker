# Reward-aware WebSocket paper evaluation

`python -m tools.maker_research websocket-paper --output /tmp/session.jsonl --program PROGRAM.json --config CONFIG.json --seconds 60`

Requires configured KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH. The command uses
market-data subscriptions only; no order placement or account trading. It
captures a bounded interval, validates the hash chain and subscription sequences,
then replays it. It is capture-then-replay, not a continuously operating trader.
Capital is limited to $40 for test sessions; explicit fee/latency/queue scenarios
are mandatory. Do not paste signing keys into source files or commit them.

Reward accrual uses actual simulated resting quantities before each event. Both
sides' cutoff/reference levels include our simulated orders. Integrals stop at
stale-book, program and pending-state deadlines; unknown activation intervals
receive no retroactive credit. Cap, floor and minimum payout apply once to the
accumulated observation window, assuming no other participation in the program.
Amounts remain modeled estimates, never credits available for compounding.
Book/trade capture does not prove market-open status; this remains an explicit
assumption requiring lifecycle integration before an operational payout claim.

Fresh run on 2026-09-21 stopped with FileNotFoundError before connecting because
no signing-key file was configured. Zero records were captured. Previously
retained development tapes were evaluated with capital_usd=35, size=1, latency
250ms, queue multiplier=1, modeled maker fee=.01 and exit fee=.02 per contract.
WEBSOCKET_REWARD_REPLAY.json retains the results. Copper join-best: -$2.08 before
rewards, modeled accrual $0.01247. Defensive: -$.01 and accrual $.00143. If stopping
then, both payouts are zero due to the $1 program minimum. Table tennis produced
no fills and sub-cent accrual. No positive edge established; no annualization or
claimed income. These tapes were inspected previously and are not a holdout.

Profit comes from net trading proceeds plus paid rewards less costs. A snapshot
projection with fixed share/uptime is not equivalent to realized episode
accrual. Larger sizes affect competition, qualification, inventory and losses;
small-order results cannot be multiplied to forecast a larger account.

Policy reference checked 2026-09-21:
https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program
