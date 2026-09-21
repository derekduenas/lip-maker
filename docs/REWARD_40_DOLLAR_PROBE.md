# $40 public-data paper probe

Ran against the public API on 2026-09-21. Retrieved 3,519 active program rows
in a complete page. Selected eight series by headline pool rate, one contract
per series, requiring at least ten minutes remaining. This is a bounded sample,
not a search proving which contract pays best across the exchange.

Requested metadata and two consecutive REST books per selected contract.
All sixteen book requests took approximately 4.5–5.1 seconds, exceeding the
2-second freshness ceiling. All are non-actionable. No credentials or order
endpoints were used. These are snapshot scenarios, not a continuous paper trader
or sequenced replay; fills and net profit are unknown.

The fixed quote grid uses best price and one/two cents deeper, paired sizes
1/5/10/20 plus maximum affordable, reserving $5 of the $40. These are mutually
exclusive alternatives. Hypothetical fresh-account analysis treats public depth
as competition; it does not reconcile the user's existing resting orders.

One snapshot scenario in KXTTELITEMATCH-26SEP202045MSIOSO-MSI had 59 contracts on
each side at YES $0.43 / NO $0.16, requiring $34.81. Assuming unchanged share and
50% qualified time over the remaining roughly 84 minutes, its modeled reward
was $7.35. That is the maximum total trading loss plus fees it could cover,
not profit. The wide spread and rapidly changing sports book make a static
projection particularly fragile. No such order was placed.

Retained public inputs, exact request timing and every candidate in
REWARD_40_DOLLAR_PROBE.json. Reproduce a new bounded sample with:
`python -m tools.reward_budget_probe --output /tmp/probe.json --budget 40 --markets 8`

Next operational requirement: sequenced, authenticated WebSocket observations
and a measured execution-cost forecast; this REST run supplies neither.
