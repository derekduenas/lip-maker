# Maker profitability research

This is an **offline research layer**, not a profitable strategy or a live execution upgrade. It does not alter runner gates, reward scoring, routing, or deployment. No account data has been evaluated by this change. Live eligibility is always false.

## Run

From the repository root, with JSON arrays of normalized records:

```bash
python -m tools.maker_research ingest --db /tmp/profit.db --events receipts.json
python -m tools.maker_research report --db /tmp/profit.db --mode paper --asof-ms 1800000000000 --books exits.json
python -m tools.maker_research replay --events episode.json --config scenario.json
python -m tools.maker_research markouts --fills fills.json --books books.json
python -m tools.maker_research evaluate --candidates candidates.json --budget-usd 100 --event-cap-usd 20
```

Output is JSON on stdout; redirect to retain reports. Inputs must be normalized explicitly. These commands neither fetch venue records nor automatically subscribe to the public trade feed. Do not infer trade aggressors from price alone. An adapter and capture completeness audit remain necessary before using venue data.

## Receipt ledger

Every receipt requires `event_id`, `source`, `market`, `program_id`, `mode` (`paper` or `live`), integer `ts_ms`, and `kind`. IDs are unique within mode. Exact replay is harmless; a conflicting receipt raises. Input batches commit individual receipts; correct invalid rows and rerun safely.

- `buy`, `sell`, `settlement`: `side`, `quantity`, `price_usd`, `fee_usd`. Dollars and quantities should be decimal strings. Settlement closes recorded lots at 0 or 1.
- `reward_credit`, `reward_estimate`, `operating_cost`: `amount_usd`. Reward estimates are incremental accounting events, not repeated cumulative snapshots. Only credits count toward profit. A normalized credit is a caller assertion; this module does not authenticate exchange statements.
- `reserve`: `amount_usd` replaces outstanding order collateral for this market/program. Update reserves when orders fill, cancel or change. Avoid counting filled collateral both as inventory and outstanding reserve.

History must start flat and contain every execution, fee, settlement and applicable cost. Short sales and transfers are unsupported. Reconcile to account statements separately. Program attribution must remain consistent when lots close; overlapping programs must not duplicate fills or rewards.

`exits.json` is a mapping from ticker to `ts_ms`, `yes_bids`, `no_bids` and `exit_fees_by_program`. Example:

```json
{"M":{"ts_ms":1000,"yes_bids":[["0.40","10"]],"no_bids":[["0.50","10"]],"exit_fees_by_program":{"P":"0.02"}}}
```

Books older than two seconds, future books, missing exit fees, or insufficient depth suppress net liquidation value. Depth is consumed across program lots in receipt-group order, not reused. Executable marks are estimates, not guaranteed fills. Dollar-hours integrate recorded inventory cost plus outstanding reserves; they are not a complete exchange margin model. Fees and costs count in net profit but not collateral dollar-hours.

## Replay

`episode.json` is one market and one episode in chronological receipt order. Each record requires unique `event_id`, `market`, integer `ts_ms`, optional consistent `episode_id`, and `kind`:

- `book`: `yes_bids`, `no_bids` as `[dollar_price, quantity]` arrays; `valid` boolean.
- `trade`: `side` (`yes`/`no`), explicit `aggressor` (`buy`/`sell`), `price_usd`, `quantity`.
- `gap`: invalidates market visibility. Gap-free recordings are still not proof of complete capture.

Example explicit scenario:

```json
{"size":"1","capital_usd":"10","latency_ms":250,"stale_ms":2000,"maker_fee_per_contract_usd":"0.01","exit_fee_per_contract_usd":"0.02","operating_cost_usd":"0","queue_multiplier":"2","max_spread_usd":"0.10"}
```

These fees are **stress assumptions**, not Kalshi's fee schedule. Supply multiple defensible scenarios. The CLI requires explicit fees, latency and queue multiplier. It compares no quoting, joining best bids, and a spread gate; these are illustrative baselines, not optimized policies.

Only sell-aggressor volume reaching our price consumes queue ahead and then fills our bid. Book touches never fill. Queue starts behind all displayed size at our price or better, multiplied by the stress factor. Missing cancellation evidence never improves queue position. Replacements and cancellations have latency, during which old orders can fill. Commands due at a timestamp activate using information available before the next record at that timestamp. This deterministic convention needs sensitivity analysis for coarse timestamps. Inventory and outstanding buys are capital bounded.

The tape is counterfactual: our orders would change market activity. No hidden liquidity, endogenous reaction, exchange outages or impact model is established. The simulator does not award rewards; it reports the credited reward required to break even after modeled exit. An incomplete exit produces null profit. A capture gap disqualifies validation even if a later book permits a numerical mark.

## Adverse selection and allocation evaluation

Markouts require fills with `episode_id`, `event_id`, `market`, `ts_ms`, `side`, `quantity`, `price_usd`. Book records use the replay book schema. They measure entry price minus full-depth gross exit price at 1, 10 and 60 seconds; the first book within 250 ms of the horizon is used. Explicit invalid observations break labels. Missing capture that was never recorded cannot be detected. Fees are excluded from this diagnostic and must be accounted separately. Do not subtract these markouts again from trading P&L that already includes adverse moves and exit costs.

The summary weights episode means equally. Its normal-approximation upper bound is descriptive, unreliable with small/dependent clusters, and does not establish independent samples or an edge.

Evaluation candidates require `market`, `underlying_event_id`, `capital_usd`, `horizon_hours`, `reward_lower_bound_usd`, `trading_pnl_lower_bound_usd`, `operating_cost_usd`, `uncertainty_allowance_usd`, `rules_verified`, `reward_receipts_reconciled`, `evaluation_split`, and `independent_episodes`. Bounds must refer to the same horizon. Trading P&L must already include all trading and unwind costs. Selection requires caller-attested verified terms, reconciled rewards, held-out evidence and at least 30 independent episodes. Thirty is a configurable-code research threshold, not statistical proof. This tool does not generate or verify those attestations/bounds. Repeated rows from one sporting event are not independent episodes.

Candidates with nonpositive conservative net are rejected. Remaining candidates are greedily ranked by conservative net per dollar-hour, subject to budget and shared underlying-event caps. This is a research heuristic, not a globally optimal allocator. It never modifies production market ranking or enables orders.

## Remaining deployment prerequisites

Capture and reconcile actual venue data; verify current program scoring/eligibility/account caps and fee schedules; construct separate training/validation periods grouped by underlying event; test multiple latency/queue/unwind scenarios; reconcile credited rewards; then review out-of-sample net profit and drawdown. No such empirical evidence is supplied by passing unit tests. The existing live interlock remains untouched.
