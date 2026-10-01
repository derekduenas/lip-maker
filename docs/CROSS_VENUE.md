# Cross-venue paper book

Kalshi and Polymarket US share one capital figure and one kill switch. Nothing here sends to production. The PM US key is treated as read-only: `PMUSAdapter.engine_place` forces paper and does not call the transport.

## Shared PM US pool

`GET /v1/incentives` repeats `rewardPool` on every market in a program window. That figure is one pool. `effective_reward_pool_usd` divides it by the number of member markets (`with_shared_pools` counts markets that share `program_id` and `period`). A lone market stays at `n_markets = 1`.

The published "$1 is not paid" check is `payable`. Whether that minimum is per program-period or per user-per-day is not verified against a statement, so the joint ranker uses the effective pool and does not run `payable`.

Maker rebates stay `0.0125 × contracts × p × (1−p)`, banker's-rounded to the cent on each fill (`mm.accounting.pm_us_maker_rebate_usd`).

The maker quotes from the signed websocket book only. A live check on 1 October 2026 found the signed REST order book on `api.polymarket.us` served from Cloudflare's cache (HIT, age 15–16s, about 30s) even when authenticated. The websocket market channel pushed the full book about 10 times a second, median age 0.16s. A REST book is not a quote. A websocket book older than 1s pulls the quote.

## Compounding

`BankrollLedger` adds realized rewards and fill P&L by venue, market, and series. `reallocate` shrinks the observed net $/day per $ toward a prior (`posterior`). Size cannot rise until `min_sample` observations (default 5). Each market is capped at `fraction × equity` (default 1/4). Equity 5% under the peak cuts every size in half. Equity 10% under the peak flattens.

`ScaleLadder` rungs are $500, $1,000, $2,500, $5,000, $10,000. A rung needs `n_days` in a row with reward/markout at least 1.5 and no kill. A miss or a kill clears the count. Book equity used for the day is the minimum of ledger equity and the current rung.

## Events

`match_markets` joins listings whose normalized title and resolution are the same. YES and NO on that event offset. A partial title overlap on the same resolution is an `UncertainMatch` and does not offset. `CrossKill.trip` cancels the Kalshi orders it was given and calls PM US cancel-all.
