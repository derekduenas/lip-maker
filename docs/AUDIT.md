# Audit — 1 October 2026

Paper and demo rails stay. Production stays disarmed. This file lists what was checked, what was wrong, and what the code does now. Official pages were read the same day: docs.kalshi.com (Trade API catalog and exchange sharding) and docs.polymarket.us (orders, incentives, rate limits). The Kalshi Help Center LIP article was read the same day.

## Entrypoint

`python -m mm.unattended --cycle RECORDING.jsonl --once` is the paper pass: selector, sizer, paper quote, per-second scorer, reconciler, series factor, compounding allocator, risk check. It does not open a socket. The systemd unit still heartbeats and refuses a production host. Droplet steps are in `docs/DROPLET.md`.

## Findings

| Sev | Finding | Fix |
| --- | --- | --- |
| high | `mm/unattended` main only wrote a heartbeat. Selector, sizer, scorer, reconciler, allocator, and risk were not on that process. A `credit` line in the recording was stored with `kind=credit`, which the reconciler rejects. | `--cycle` runs `mm.cycle.run_paper_cycle` on a JSONL recording. `entry_kind` / `reward_kind` becomes the ledger kind. Regression: `test_paper_cycle_on_a_recording`. |
| high | `reset_for_market` dropped resting orders and left their capital reserved. | It releases the reservation. `test_reset_for_market_releases_the_reservation`. |
| high | `reconcile` skipped every market when any ticker was uncertain. | Startup failure still blocks all markets. A later failure blocks that ticker. `test_uncertainty_on_one_market_does_not_block_another`. |
| high | `run_paper` stored fill statuses in the same dict `_expected_fills` reads by ticker, so realized fills were invisible. | Status totals stay in `fill_counts`. Applied fills increment `fill_counts_by_ticker`. `test_fill_status_counts_do_not_replace_ticker_counts`. |
| high | PM US signed REST order book on `api.polymarket.us` is a Cloudflare cache. A live check on 1 October 2026 saw `HIT` with age 15–16s, on a cache of about 30s, with the request authenticated. The signed websocket market channel pushes the full book about 10 times a second (median age 0.16s). The maker path still quoted the REST book when the websocket was missing or older than 10s, and the live cross check read REST BBO. | Quotes come only from the websocket book. A REST book is a pull. A websocket book older than 1s is a pull. Live placement refuses a target without that fresh book. `test_rest_order_book_is_never_a_quote`, `test_websocket_book_older_than_one_second_pulls`, `test_fresh_websocket_book_ignores_the_rest_book`. |
| high | Three hours of paper sim, 7,035 fills. Quoting into the last hour before close lost about -$924/day per $1,000 of fills. Pulling 15 minutes before close flipped that to about +$238. Nothing cancelled on `close_time`. | Hard cancel of that market at T-15 minutes ( `LIP_PULL_BEFORE_CLOSE_MIN`, default 15). The quoter cancels in `reconcile`. The supervisor writes `cancel TICKER` even when the heartbeat is fresh. `test_quoter_cancels_inside_the_close_window`, `test_supervisor_cancels_a_market_inside_the_close_window`. |
| high | Hourly temperature (`KXTEMP...H`) and 15-minute (`*15M`) series were eligible for the selector. | Excluded unless `allow_intraday=True`. `test_hourly_and_fifteen_minute_series_stay_out_unless_enabled`. |
| high | Eleven paper fills lost more than the premium cap on a single fill. Size was not checked against that loss before the order. | Worst case of one fill is the quoted premium (the contract settles at 0). Size is cut so that premium stays within $100 (`LIP_SINGLE_FILL_CAP_USD`). Selector, sizer, and quoter all apply it. `test_single_fill_cap_sizes_the_quote_before_it_is_sent`. |
| high | The live selector had no per-series go/no-go. A series could be traded with a short sample, a loss, a 5-minute markout that ate the reward, or a result that died if the reward were halved. | Live `allocate` trades a series only when it has at least 5 days, at least 30 settled fills, net strictly positive, 5-minute markout cost per fill strictly under reward per fill, and stays strictly positive after a 50% reward haircut. A missing record is no-go. Paper does not apply the gate. `test_live_selector_trades_only_a_series_that_passes_the_gate`. |
| medium | Websocket applied deltas before any snapshot. The accrual book already refused those. | `KalshiWS` drops them. `test_delta_before_snapshot_is_dropped`. |
| medium | `KalshiClient.get_balance` ignored `balance_dollars` and divided `balance` by 100. | `parse_balance_usd` prefers dollars, then cents. `test_balance_dollars_wins_over_the_cents_field`. |
| medium | Live quote-manager decrease and amend omitted `exchange_index`, which the V2 docs default to shard 0. Parsed portfolio rows never stored the shard, so a rehydrated order still omitted it. | The body includes it when the resting order has one. `_parse_live_order` and a create response keep `exchange_index`, and resync copies it. `test_decrease_posts_exchange_index`, `test_parsed_resting_order_keeps_its_shard`. Adapter `amend` takes the same argument. `test_amend_posts_the_shard_it_was_given`. |
| medium | Selector quoted the touch. The sizer quoted the target/5 reference. On a deep book those prices differ. | Selector uses the reference when the book reaches target/5, and the touch when it does not. `test_quote_price_is_the_reference_when_the_book_has_one`. |
| medium | `BookDriver` ignored a NO-side reference move whenever the YES reference existed. | Each side has its own gate. `test_no_side_reference_move_requotes`. |
| medium | PM ranker did not pass Max Spread and scored an empty book. | `PMQuote.max_spread_usd` and competing orders are passed through. `test_pm_max_spread_and_competition_change_the_reward`. |
| medium | PM incentives GET ignored the documented 5/s budget. | Separate token bucket. `test_incentive_fetch_stops_at_five_per_second`. |
| medium | Kalshi amend and decrease did not spend the write token bucket. A 429 had no wait. | Both spend 10 tokens. HTTP 429 returns `backoff_s`. `test_amend_respects_the_token_bucket_and_429_reports_backoff`. |
| medium | `paper_fills` hardcoded the production trade host. | `trades_url` takes a base and otherwise reads `settings.KALSHI_API_BASE`. `test_trades_url_uses_the_caller_base`. |
| medium | Discovery dropped `max_reward_per_account`. | Parsed from centi-cents into `max_reward_usd`. `test_program_parse_converts_the_account_cap`. |
| medium | `LIP_RAMP_PHASE=later` crashed at import with `ValueError`. | A `RuntimeError` names the variable. `test_ramp_phase_rejects_text`. |
| low | No startup check for a key pasted on argv, no rotating log, no local status, no clock-skew gate, no droplet script. The unattended unit did not load `/etc/lip-maker/lip-maker.env`. | `mm/ops.py`, `mm/status_page.py`, `deploy/droplet/setup.sh`, `docs/DROPLET.md`. The unit has `EnvironmentFile=-/etc/lip-maker/lip-maker.env` and still sets `LIP_PAPER=true`. Tests cover skew, redaction, the status JSON, and the script text. |

## Accepted, not changed

* `engine_place` latches `paper=True` on that PM adapter. The engine key is read-only. A later `place` on the same object stays paper.
* `Health` does not kill when markout cost was never recorded. The ratio is unknown. A recorded cost with reward/markout under 1 still kills.
* `RiskEngine.check_quote` blocks a new quote on a limit breach and sets `cancel_all` on a kill (daily loss, disconnect, fill-rate, already dead). A soft limit is not a flatten.
* `CrossKill.trip` still marks the book killed and calls both cancels. Waiting for both acks before tripping would leave the book running when a cancel fails.
* The sizer's objective is reward minus a flat markout. `quote_economics` also subtracts fees and holding. They meet on price (the reference) and still differ on costs. The cycle uses the sizer for the quote size and the risk engine for the dollar check.
* `run_paper.py` remains the long-running discovery and quote process. The cycle is the single paper entry that chains the newer modules. The heartbeat unit does not quote.

## Unverifiable against the docs fetched 1 October 2026

* No Kalshi route returns our liquidity award or a per-period score. `GET /incentive_programs` is the pool, `paid_out`, and optional `max_reward_per_account`. Settlements, fills, and balance are not that credit.
* The Help Center LIP article does not publish a top-N participant cap. The account cap in code is `max_reward_per_account` when the program row has one.
* The 28 February 2026 scoring formula is not reimplemented. Programs that start before 30 July 2026 are tagged and not scored with the current formula.
* Polymarket US: whether the $1 minimum is per program period or per user per day is not stated on the incentives page we fetched. The ranker does not apply `payable`.
* The PM US shared-pool divisor (API pool divided by member markets) is not checked against a payout statement.
* PM US assumption A1 in `pm_us_lip_scorer` (the second splits equally across the two sides) is labeled there as an assumption.
* International CLOB rewards are a different formula. They are not used for PM US.

## Still off

Maker-only acknowledgement, `allow_production`, and `LIVE_ARMED` stay off. The droplet script does not turn them on.
