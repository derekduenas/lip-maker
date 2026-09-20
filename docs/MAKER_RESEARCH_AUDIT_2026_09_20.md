# Research audit and completion round — 2026-09-20

Audited baseline: 4014a2ed07474be7431e5ec6152eb0f6ab9594e3.

## Fixed findings

1. Ledger valuation ignored an explicit invalid-book flag. Invalid books now suppress executable net value.
2. Candidate attestations accepted truthy strings. Verification flags now require literal booleans; episode counts require integers. Duplicate market/event candidates cannot consume capital twice.
3. Delayed trade notifications could fill an order activated after the trade actually executed. Exchange time now limits eligibility for simulated fills.
4. Shared REST/WS signatures used maximum-length PSS salt. Both now use the documented SHA256 digest length, verified locally against a 32-byte-salt public-key verifier. Production authentication was not exercised.
5. The existing book parser depended on an unpinned legacy pricing default. Its subscription now explicitly requests separate leg pricing. The new independent recorder uses explicit unified pricing and decimal normalization. This legacy production opt-out will need revisiting if the exchange removes it.
6. Replay book-validity strings are rejected instead of treated as booleans.
7. Two obsolete coffee/sugar tests still expected task #102 to be unimplemented. Updated them to assert the already-shipped unreliable-feed cap and added inclusive boundary/below-cap checks. Production risk policy is unchanged.

8. Replay could reuse pre-trade exit depth. Open inventory now requires a subsequent book observation before liquidation valuation; otherwise net is unknown. The synthetic fixture explicitly supplies that later book.

## Implemented missing workflows

- Bounded standalone public WS capture, raw receipt retention, integrity manifest, strict normalization and export.
- Atomic statement-based account reward reconciliation, economic-ID deduplication, source hashing, total control and discrepancy reporting.
- Frozen chronological policy comparison with underlying-event separation, cost/latency/queue scenarios and explicit blockers.

No production scoring, market ranking, risk interlocks or deployment was enabled or promoted. The recorder has no order submission path. No new hosting or feeds were purchased.

## Profitability evidence available here

The two local databases contained no fills, receipts or capture history. No Kalshi credential environment variables were configured. A bounded public incentive API request returned HTTP 403. A local capture initialization returned BLOCKED / FileNotFoundError with zero records; it did not reach a venue subscription. These are environment/access findings, not proof the exchange is unavailable generally.

There is no actual account statement in this workspace. The CSV adapter is tested against synthetic statements; it does not invent an undocumented account reward API or claim to have reconciled actual account payments.

The retained synthetic cost attack has one development episode and one held-out episode. Joining best bids and the spread gate each lose $0.03 per episode under the worst supplied scenario; doing nothing is $0.00. No policy is selected. This confirms cost-loss rejection, **not empirical NFL performance**. No strategy has demonstrated a positive net edge in this round.

## Sources checked

- [Kalshi trade direction and price convention](https://docs.kalshi.com/getting_started/order_direction)
- [Kalshi public trade fields](https://docs.kalshi.com/websockets/public-trades)
- [Kalshi book snapshot and delta fields](https://docs.kalshi.com/websockets/orderbook-updates)
- [Kalshi signing example](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
- [Incentive program metadata](https://docs.kalshi.com/api-reference/incentive-programs/get-incentives)

These sources establish wire conventions and metadata, not a verified account payout formula. Full current program terms, coverage-complete capture, actual statement reconciliation and out-of-sample reward-adjusted economics remain required before profitability can be established.
