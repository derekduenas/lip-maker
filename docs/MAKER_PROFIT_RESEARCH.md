# Maker profitability research

This is an **offline research layer**, not a profitable strategy or a live execution upgrade. It does not alter runner gates, reward scoring, routing, or deployment. Shared authentication now uses the documented SHA256 digest-length RSA-PSS salt; the existing runner explicitly requests legacy leg book prices, while the standalone recorder requests unified YES prices. No account data has been evaluated by this change. Live eligibility is always false.

## Run

From the repository root, with JSON arrays of normalized records:

```bash
python -m tools.maker_research ingest --db /tmp/profit.db --events receipts.json
python -m tools.maker_research report --db /tmp/profit.db --mode paper --asof-ms 1800000000000 --books exits.json
python -m tools.maker_research replay --events episode.json --config scenario.json
python -m tools.maker_research markouts --fills fills.json --books books.json
python -m tools.maker_research evaluate --candidates candidates.json --budget-usd 100 --event-cap-usd 20
```

Output is JSON on stdout; redirect to retain reports. Inputs must be normalized explicitly. The `capture` and `export-capture` commands below now record and normalize the public book/trade feed. Account reward ingestion uses an explicitly mapped CSV statement; no reward-payment API is assumed. Do not infer trade aggressors from price alone.

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

The tape is counterfactual: our orders would change market activity. No hidden liquidity, endogenous reaction, exchange outages or impact model is established. The simulator does not award rewards; it reports the credited reward required to break even after modeled exit. An incomplete exit produces null profit. Open inventory also requires a book observation after the latest trade; pre-trade depth is not reused for exit valuation. A capture gap disqualifies validation even if a later book permits a numerical mark.

## Adverse selection and allocation evaluation

Markouts require fills with `episode_id`, `event_id`, `market`, `ts_ms`, `side`, `quantity`, `price_usd`. Book records use the replay book schema. They measure entry price minus full-depth gross exit price at 1, 10 and 60 seconds; the first book within 250 ms of the horizon is used. Explicit invalid observations break labels. Missing capture that was never recorded cannot be detected. Fees are excluded from this diagnostic and must be accounted separately. Do not subtract these markouts again from trading P&L that already includes adverse moves and exit costs.

The summary weights episode means equally. Its normal-approximation upper bound is descriptive, unreliable with small/dependent clusters, and does not establish independent samples or an edge.

Evaluation candidates require `market`, `underlying_event_id`, `capital_usd`, `horizon_hours`, `reward_lower_bound_usd`, `trading_pnl_lower_bound_usd`, `operating_cost_usd`, `uncertainty_allowance_usd`, `rules_verified`, `reward_receipts_reconciled`, `evaluation_split`, and `independent_episodes`. Bounds must refer to the same horizon. Trading P&L must already include all trading and unwind costs. Selection requires caller-attested verified terms, reconciled rewards, held-out evidence and at least 30 independent episodes. Thirty is a configurable-code research threshold, not statistical proof. This tool does not generate or verify those attestations/bounds. Repeated rows from one sporting event are not independent episodes.

Candidates with nonpositive conservative net are rejected. Remaining candidates are greedily ranked by conservative net per dollar-hour, subject to budget and shared underlying-event caps. This is a research heuristic, not a globally optimal allocator. It never modifies production market ranking or enables orders.

## Remaining deployment prerequisites

Capture and reconcile actual venue data; verify current program scoring/eligibility/account caps and fee schedules; construct separate training/validation periods grouped by underlying event; test multiple latency/queue/unwind scenarios; reconcile credited rewards; then review out-of-sample net profit and drawdown. No such empirical evidence is supplied by passing unit tests. The existing live interlock remains untouched.


## Automatic capture and export

```bash
python -m tools.maker_research capture --market ACTUAL_MARKET_TICKER --seconds 300 --output /private/path/game.jsonl
python -m tools.maker_research export-capture --capture /private/path/game.jsonl --episode-id GAME_SESSION > /private/path/episode.json
```

Run capture in the environment containing the existing Kalshi key configuration. It is a separate bounded process (up to one hour), connects with authentication and subscribes only to public orderbook/trade channels. It never submits, cancels or modifies orders. Do not supply a series ticker in place of a market ticker. Blocked captures return exit code 2. Files are exclusive-created; choose a new path for each session. Do not commit captures or account statements.

Raw messages retain receipt wall time, monotonic time, sequence, and a chained content hash. A sidecar manifest is incomplete until the recording finishes. Disconnects/errors stop the session; there is no silent reconnect across a gap. Export checks the chain, record count, both subscription acknowledgements, per-subscription sequence continuity, snapshot-before-delta, prices, quantities, directions and timestamps. Checksums detect accidental alteration, not malicious replacement of both data and manifest. They do not prove the venue delivered every event. Local receipt latency and clock synchronization still require measurement.

The capture requests `use_yes_price=true`; NO book levels are complemented into NO-token prices during export. Sub-cent research data is retained as Decimal. This does not enable sub-cent production quoting. Public long-NO aggressors consume YES bids; long-YES aggressors consume NO bids. Only canonical direction fields are accepted. Block trades, missing block classification, conflicting fields and future exchange timestamps fail export. Trades executed before a simulated order became active cannot fill that order merely because their notification arrived late.

## Statement reward reconciliation

```bash
python -m tools.maker_research reconcile-rewards --db /private/path/profit.db --statement /private/path/rewards.csv --mapping /private/path/columns.json --account-id ACCOUNT --expected-total-usd 12.34
```

The mapping JSON maps each required canonical field to an actual CSV column name:
`credit_id`, `market`, `program_id`, `ts_ms`, `amount_usd`, `account_id`, `status`, `kind`.
This is our adapter contract, **not a claim about Kalshi's export column names**. Timestamps are integer epoch milliseconds and money is USD. Supply only rows whose status is `posted` and kind is `liquidity_reward`; mixed transaction exports need an explicit verified conversion. No conversion may substitute a market/program pool for an account receipt. If a statement has no program attribution, do not invent it: reconciliation remains blocked until a defensible mapping is available.

Compare `expected-total-usd` to an independently checked statement total, not a total blindly recalculated from these same input rows. Import is atomic: a mismatch, conflicting existing credit, duplicate, wrong account or unsupported reversal writes no credits. Re-exported economic IDs do not double-credit even when CSV formatting changes. Source hashes and column mappings are retained in SQLite. Use a separate database per account. No paper reward credits are created from live statements.

The output compares lifetime recorded credits and incremental estimates per market/program through the latest imported credit timestamp. It is a discrepancy report, not automatic proof of entitlement or complete account history. Authenticity, coverage, fee treatment and current incentive terms still need verification. Posted credits may differ from accrual estimates because of payment timing.

## Chronological profitability attack

```bash
python -m tools.maker_research attack --episodes tests/fixtures/maker_research/synthetic_episodes.json --scenarios tests/fixtures/maker_research/stress_scenarios.json --cutoff-ms 1000
```

This example is synthetic. Its retained result is `docs/MAKER_SYNTHETIC_COST_ATTACK.json`.
For actual research, the episodes file is an array of objects containing `episode_id`, `underlying_event_id`, and exported `events`. Every event must carry the same episode identity. Episodes are flat-start counterfactual experiments; they do not assert the actual account started flat. Group correlated markets and sessions using the same underlying-event ID. An episode may not straddle the cutoff and an underlying event may not occur in both splits.

Declare the cutoff and the full array of scenario configurations before inspecting held-out outcomes. The workflow hashes the complete input, compares the three fixed policies, chooses only from development results, then reports held-out outcomes. Gaps, incomplete liquidation, a missing split or nonpositive development net prevent selection. Costs are scenario inputs, not a verified fee model. Episode means and worst-scenario means are descriptive, not compounded portfolio returns. There is no statistical promotion gate; live eligibility remains false even with positive modeled results. If the held-out result informs a code or parameter change, that data becomes development data for the next run.

Actual credited rewards are not automatically assigned to alternate simulated policies. The attack reports break-even rewards separately. To establish a reward-dependent edge, collect real paper quote eligibility and actual account receipts under a fixed policy, verify program scoring and align economic windows. A simple positive modeled result is insufficient.
