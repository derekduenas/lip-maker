# Gold-standard design for a small-operator prediction-market maker

Retrieved **1 October 2026**. Dollar figures in the 30 September 2026 audit
(`uploads/REPORT.md` in the task packet; the repo copy is the branch this
work builds on) are the constraint: paid Kalshi liquidity rewards on the
order of a few hundred dollars were smaller than named adverse-selection
losses. The architecture below is the one a small operator can actually run.
It is not a latent-order-book HFT stack, and it does not pretend a 30-second
poll is a strategy.

This document cites the page or paper that was opened. Where a coefficient
is still an operator input, it says so.

## 1. What "good" means on these venues

A liquidity-incentive maker is paid for resting size near a reference price,
and it is picked off when that price is wrong. Net expected yield of a pool
is reward share plus maker rebate, minus expected adverse selection, minus
fees, minus the cost of holding inventory to settlement, divided by the
capital that inventory ties up. Quoting a pool with a negative number is how
the April–May 2026 book lost money on long-dated and information-sensitive
binaries while commodity weeklies with an outside reference made money.

Two venue facts fix the objective:

* **Kalshi Liquidity Incentive Program.** Help Center, "Liquidity Incentive
  Program", <https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program>
  (page text retrieved 1 October 2026). A random snapshot about once a
  second. The reference price is the first level, walking down from the best
  bid, at which cumulative resting size reaches one fifth of target size. Size
  at or better than that reference scores in full. Deeper size scores
  `discount_factor ^ ticks`. A side that never reaches target size scores
  nothing. Both sides of the book must qualify for a snapshot to pay. The
  August 2025 regulatory notice
  (<https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Volume%20and%20Liquidity%20Incentive%20Program%20-%20August%202025.pdf>)
  described an older procedure that initialised the reference at the best
  bid. The live help-center text is the one `engine/lip_scorer.py` follows.
  Where to read live parameters:
  <https://help.kalshi.com/en/articles/16076644-liquidity-incentive-programs-where-to-find-them>
  (updated 26 July 2026 in the help center; the public trade API serves the
  program rows). Payouts under $1 are not paid. The program help text, as
  fetched for the 30 September audit, runs through 1 January 2027.

* **Polymarket US liquidity incentives.**
  <https://docs.polymarket.us/incentives/liquidity> (fetched 1 October 2026).
  Score = `discount_factor ^ (ticks from best price) × size`. The walk uses
  raw size out to target size. Optional max spread is a test of the book,
  not of each trader: both size-adjusted prices must sit within max spread
  of their midpoint or the second pays nobody. One-sided quotes earn when
  the second qualifies. Live parameters:
  `GET https://gateway.polymarket.us/v1/incentives`, 5 requests/second
  (<https://docs.polymarket.us/api-reference/incentives/overview>, fetched
  1 October 2026). Rewards are calculated within 5 business days of the
  period and credited within 2 more. Under $1 is not paid. Cancelled or
  postponed games pay nothing.

The international Polymarket quadratic `Q_min` formula is a different
program. It is not used here.

## 2. Fair value, inventory, toxicity, queue, pull

### 2.1 Reservation price

Avellaneda and Stoikov, "High-frequency trading in a limit order book",
*Quantitative Finance* 8(3), 217–224, 2008
(<https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf>,
<https://math.nyu.edu/~avellane/HighFrequencyTrading.pdf>):

```
r(s, q, t) = s − q γ σ² (T − t)
δ_bid + δ_ask = γ σ² (T − t) + (2/γ) ln(1 + γ/k)
```

`s` is a reference value, not "the best bid". `q` is inventory. The bid and
ask are placed around `r`, so a long inventory lowers the price at which the
maker is willing to buy. Ho and Stoll, "Optimal dealer pricing under
transactions and return uncertainty", *Journal of Financial Economics* 9(1),
47–73, 1981, is the inventory model this formalises.

Guéant, Lehalle and Fernandez-Tapia, "Dealing with the inventory risk",
arXiv:1105.3115 (2011; *Mathematics and Financial Economics*, 2013,
<https://arxiv.org/html/1105.3115v5>): the same control problem with a hard
inventory bound. At the bound the maker stops quoting the side that would
increase inventory. The asymptotic quotes are characterised from a linear
ODE system; the practical consequence this codebase uses is the bound
itself, not a spectral expansion.

**Units.** The formulas are in one price unit. Feeding σ in cents and
`(T−t)` in years makes the skew a fraction of a cent, so the quote never
leaves the touch and "inventory control" silently becomes size-only. That
was the failure mode in the audit. `mm/reservation.py` measures:

* `s` and `r` in YES cents
* `q` as inventory divided by the per-market net cap (dimensionless, clipped)
* `σ` as the hourly standard deviation of that YES value, in cents
* `τ` in hours, floored at one minute
* `γ = 0.04` per cent, so a full long position, σ = 5¢ per √hour and τ = 1
  hour, moves `r` by `1 × 0.04 × 25 × 1 = 1` cent

The skew is applied to **price**: the YES bid moves by the whole-cent gap,
and the NO bid moves the other way (a higher NO bid is a lower YES offer).
At `|q| ≥ 1` the increasing side is not quoted. The textbook half-spread is
computed and returned; on a liquidity-incentive book it is a ceiling, not a
reason to abandon the reducing side's reward score. Distance from the
scoring reference is itself a cost.

### 2.2 Where `s` comes from

The book mid is the crowd's last trade. It is the wrong `s` when the
underlying has already moved. `mm/fair_value.py` does three things, and it
refuses a fourth:

1. **No model inputs.** Use the book mid. Do not invent a probability.
2. **Spot, strike, and a horizon sigma.** Bachelier digital
   `Φ((S−K) / (σ√τ))`, blended equally with the book. σ√τ is the caller's
   number; a non-positive sigma is rejected.
3. **A jump, or a weather observation that disagrees with the forecast by
   2 degrees or more.** Pull. Do not map degrees or a gap-without-vol into
   a made-up cent price.
4. Not done: a fitted implied-vol surface, a sportsbook, or a news classifier.
   Those need data this process does not have. A pull hook is the extension
   point.

Glosten and Milgrom, "Bid, ask and transaction prices in a specialist market
with heterogeneously informed traders", *Journal of Financial Economics*
14(1), 71–100, 1985: the spread is adverse selection, not just inventory.
An external print the other side can see is that information.

### 2.3 Markout

The markout of a fill is the signed change in the mid after the fill, from
the maker's side. Positive means the price moved in our favour. Huang and
Stoll, "Dealer versus auction markets", *Journal of Financial Economics*
41(3), 313–357, 1996, measure the same object as the realized half-spread:
the effective spread minus the subsequent price move. `engine/adverse_selection.py`
already stores 5s / 30s / 120s markouts. The pool selector takes
`E[markout]` in cents per contract as an input. It does not fit a new model
in-process. A logistic regression on `P(markout_30 < −1¢)` is the right next
estimator once there are thousands of labelled fills; it is not shipped here
as a fitted model, because fitting one on the current sample would be a
number we invented.

### 2.4 Queue position

Moallemi and Yuan, "A Model for Queue Position Valuation in a Limit Order
Book", Columbia Business School Research Paper 17-70, SSRN 2996221
(<https://moallemi.com/ciamac/papers/queue-value-2016.pdf>). Under price-time
priority the value of an order at queue position `q` splits into a fill
probability `α(q)` and an adverse-selection cost `β(q)` that rises as `q`
gets worse, plus the option to move up the queue if the order is left in
place. On large-tick names they find queue value on the order of the
half-spread. Prediction-market books are large-tick (1¢ on a 0–1 contract).
Cancelling to re-place at the same price donates that value.

Kalshi states the operational consequence directly. Amend Order (V2),
fetched 1 October 2026,
<https://docs.kalshi.com/api-reference/orders/amend-order-v2>:

> Amending only expiry or decreasing size preserves queue position.
> Increasing size or changing price forfeits queue position and places the
> order at the back of the queue.

Decrease Order (V2),
<https://docs.kalshi.com/api-reference/orders/decrease-order-v2>, is the
size-down call (`reduce_to` or `reduce_by`, exactly one). Queue position is
readable at
`GET /portfolio/orders/{order_id}/queue_position`
(<https://docs.kalshi.com/api-reference/orders/get-order-queue-position.md>):
`queue_position_fp` is the contracts ahead of us.

Polymarket US batch modify is documented as cancel-replace
(<https://docs.polymarket.us/api-reference/orders/overview>, fetched
1 October 2026). The single-order modify page does not say the queue is
kept. This code therefore never claims a PM US modify kept its place.

### 2.5 When to pull

Pull, do not widen, when any of these is true:

* the external reference jumped more than `k` horizon-sigmas (default 3),
  or a weather observation gap is at least 2 degrees
* the in-loop adverse-selection guard already says pull (fill burst,
  markout EWMA, volatility) — that guard stays the circuit breaker
* the book is stale or the socket has been down (section 4)
* the risk engine has latched a kill (section 5)

A resting quote in a fast book is the order informed flow hits. Skipping
the *reprice* and leaving the old order up was the volatility bug in the audit.

## 3. Process shape

One writer per venue owns the order book in memory. Network reads are
applied by that writer. Reconciliation treats the venue as truth for
existence, remaining size and price:

* local working order absent on the venue → cancelled (or rejected, if we
  never got an ack)
* venue order we do not know → adopted, not ignored
* size or price differs → overwritten from the venue

Illegal local transitions raise. The legal ones are the usual lifecycle:
pending-new, resting, partial, pending-amend, pending-cancel, filled,
cancelled, rejected, plus `unknown` when a write timed out after it may
have been accepted. An unknown submit keeps its capital reservation until
reconciliation says what happened. That rule already exists in
`execution/quote_manager.py`; the state machine in `mm/order_machine.py`
is the same rule in a venue-neutral type.

The adapter interface (`mm/venues`) is `place`, `decrease`, `amend`,
`cancel`, `cancel_all`, `queue_position` where the venue has it, and
`incentives` where the venue publishes them. Kalshi and PM US are
implemented. ForecastEx is registered and refuses every write: IBKR
NTM 2026-139 (member-customer market-maker program, 20 May 2026) and
NTM 2026-186 / 2026-191 (liquidity retainer, 21 July 2026, amended
23 July 2026) are real programs, and this repo does not have the
retainer exhibit or a member session. Inventing a FIX session would be
worse than a stub.

Rate budgets mirror published costs, they do not guess:

* Kalshi Create Order (V2) default cost is 10 tokens; Cancel Order (V2) is
  2 tokens (both pages fetched 1 October 2026). Legacy mutation routes were
  priced at 10× those costs before removal.
* PM US retail: 20 requests/second per API key, HTTP 429
  (<https://docs.polymarket.us/api-reference/rate-limits>). The institutional
  trader guide states a separate 100 requests/second firm average
  (<https://docs.polymarket.us/trader-guide/rate-limits>). This repo's PM
  client is the retail API, so the adapter's bucket is 20/s. Incentives are
  a further 5/s. A 5-second latency stopgap rejects new orders and
  cancel-replaces that sit unprocessed; pure cancels are exempt. Do not
  treat that reject as a reason to stop cancelling.

## 4. Risk

`mm/risk.py` is the single check. Worst-case dollars, not a flat 50¢:

* per market, per series, per underlying, per venue, gross account
* underlying means a shared print: Brent daily and Brent weekly, a BTC
  ladder, a weather regime. Unrelated event tickers are **not** one factor.
  A "Trump cluster" is not inferred from the ticker string.
* daily loss is realized plus mark-to-market, supplied by the caller. The
  engine does not mark books it has not been given.
* at capital ≤ $1,000 the limits are the small-live set: daily loss $40,
  $50 per market, $150 per underlying. Above that, 5% of capital capped by
  the ramp table in `config/constitution.py`.
* `MAX_FILLS_PER_MINUTE` (30) latches a kill. It does not quietly re-arm
  when the minute expires. `risk/sentinel.py` reads the same clock, and
  `PaperRunner.on_fill` records into it.
* a disconnect of 3 seconds or longer, or process exit, latches
  `cancel_all`. The engine does not send the cancels; the venue loop does.
  Kalshi's existing `pull_all_exposure("ws_disconnect")` path remains.

Exchange-side protection, where it exists:

* **Kalshi order groups.**
  <https://docs.kalshi.com/getting_started/order_groups> (fetched
  1 October 2026). `POST /portfolio/order_groups/create` with
  `contracts_limit` in 1..1,000,000. Fills in a rolling 15-second window
  over the limit cancel every order in the group and reject new ones until
  reset. One group per market, limit equal to the per-market contract cap.
  This still works if our process is dead.
* **Kalshi FIX** `CancelOrdersOnDisconnect` (tag 8013) is a session flag,
  not a REST flag
  (<https://docs.kalshi.com/fix-margin/authentication>). This repo speaks
  REST. REST protection is the order group plus an explicit cancel-all.
* **PM US** has no order-group equivalent in the docs fetched above.
  `POST /v1/orders/open/cancel` with `{"slugs": [...]}` or an empty list
  for everything
  (<https://docs.polymarket.us/api-reference/orders/cancel-all-open-orders>).

`MAKER_ONLY_ENFORCEMENT_VERIFIED` stays `False`. Every live write in the
new adapters calls `require_live_execution_allowed()` and therefore does
not send. Paper is the default (`LIP_PAPER` defaults true; live also
requires the ack phrase in `config/settings.py`).

## 5. One-sided inventory

A Kalshi contract and a PM US contract are a hedge only when an operator
has recorded that they share source, strike, timestamp, and postponement
rules. The default is deny. PM US settles postponed games at the last fair
market price and pays no incentive; Kalshi rules for the "same" event are
not that sentence. `mm/hedge.py` looks up an explicit allow-list.

If `|inventory|` is inside the soft cap, or the fill is not flagged toxic,
do nothing. If the pair is not equivalent, quote only the reducing side.
If it is equivalent, simulate a taker hedge only when

```
|fair − hedge_price| × contracts + taker_fee < expected markout loss
```

The decision is logged, including the price and the fee the simulation
would have paid. Nothing is sent. PM US taker fee and maker rebate, from
<https://docs.polymarket.us/fees> fetched 1 October 2026, effective
00:00 ET on Friday 25 September 2026:

```
fee or rebate = Θ × C × p × (1 − p)
Θ_taker = 0.0695          (max $1.74 per 100 contracts at p = 0.50, before rounding)
Θ_maker = 0.0125          (a credit; max $0.31 per 100 at p = 0.50)
```

Banker's rounding to the cent (half to even). The page's own example:
1,000 contracts at $0.10 costs the taker $6.26 and pays the maker $1.12.
A table-tennis coefficient of 0.10 is announced for 23:59 ET on 30 September
2026 and is **not** the schedule the page says is in effect for the general
book. `mm/accounting.py` uses 0.0695 / 0.0125.

Kalshi taker and maker, fee-schedule PDF "July 2026 — 7.7.26 update",
<https://kalshi.com/docs/kalshi-fee-schedule.pdf>, and the series schema at
<https://docs.kalshi.com/api-reference/market/get-series.md> (both retrieved
1 October 2026):

```
taker = round_up(M × 0.07 × C × P × (1−P))
maker = round_up(M × 0.0175 × C × P × (1−P))    # only on series that charge makers
```

`fee_type` from `GET /series/{ticker}`:

| fee_type | maker coefficient |
|---|---|
| `quadratic` | 0 (14,355 of 14,518 series on the 30 September pull) |
| `quadratic_with_maker_fees` | 0.07 × 0.25 = 0.0175, times `fee_multiplier` |
| `quadratic_with_combo_maker_fees` | 0.07 × 0.50 = 0.035, times `fee_multiplier` |

The series schema states the combo maker multiplier is 0.5 rather than the
standard 0.25. Charging 0.0175 on combo series understated that fee.
Charging 0.07 on every series, which `engine/fees.py` still does as a
conservative fallback, overstated it on `quadratic` series. `engine/series_fees.py`
and `mm/accounting.kalshi_fee_usd` use the table above. Rounding in code
remains ceil to $0.000001, which is the rule at
<https://docs.kalshi.com/getting_started/fee_rounding> already verified in
`engine/fees.py` on 20 September 2026. The PDF's "centicent" wording is not
what that page says, so the code did not switch. The schedule objects stay
`verified=False` until an operator records a fill-level comparison against
a statement.

External hedges (IBKR futures, Kraken spot) stay behind the existing
`AUTO_HEDGE_*` flags, all default off.

## 6. Pool selection

```
NEY = (reward_share + maker_rebate − adverse_selection − fees − holding) / capital
```

`reward_share` is an output of the venue scorer the caller already trusts.
This module does not re-derive Kalshi or PM US scoring. Holding is a per-day
penalty on expected fills after the first day, smaller when an external
reference exists. Exclusion:

* unknown settlement time → out
* more than 45 days → out, reference or not
* more than 14 days and not a commodity, crypto, or weather market with an
  observation → out

A referenced market receives +0.01 NEY. That wins a tie and a one-percent
gap. It does not promote a toxic commodity over a clean short-dated book.
`mm/pool.select` returns at most N eligible markets (default 10, the
small-live count).

Estimated reward dollars are stored on `RewardBook.estimated_usd`. Paid
dollars require a source in `engine.reward_provenance.PAID_SOURCES`
(`kalshi_statement`, `kalshi_api`, `operator_receipt`). Cash P&L is
realized + paid + maker rebate − fees. The estimate is printed on the daily
report on its own line and is not in the cash number.

## 7. Capital

`mm/bankroll.capital_usd` is the only capital figure:

1. `LIP_BANKROLL` if set
2. else `LIP_ACCOUNT_USD`
3. else $5,000, which is what `engine.account_ledger` already opened

`config.settings.BANKROLL_USD` and `ACCOUNT_OPENING_CASH_USD` are that
number. The old $80 default was a second account. It made the census report
three feasible programs and it made a 5% daily-loss cap $4 while the
constitution's ramp-4 cap was $250. The quote-manager daily-loss backstop
is now the tighter of 5% of this capital and the ramp cap. Paper gross caps
in `settings.py` are unchanged (they are the hardcoded paper block). Live
gross caps scale with the unified capital, and live sending is still blocked.

## 8. Record, replay, report

`mm/recorder.py` appends one JSON object per line (`book`, `quote`, `trade`,
plus `settlement` and `estimate` for the replay). `mm/replay.py` feeds those
quotes and trades to `execution.paper_fills.PaperFillSimulator`. That
simulator is the paper fill rule already in the runner: we join the back of
the queue at our price, trades timestamped before activation do not fill us,
and only a print at our price on the opposing taker side consumes us. A
second, kinder fill model would not be a test of this system. Marks use a
`settlement` record when the file has one, otherwise the last YES mid. The
daily report (`mm/report.py`) prints cash and the estimate on separate lines.

## 9. Kalshi order API, as of 1 October 2026

Create Order (V2), OpenAPI 3.32.0,
<https://docs.kalshi.com/api-reference/orders/create-order-v2>:

> The legacy `/portfolio/orders` endpoint will be deprecated no earlier than
> May 6, 2026 — clients should migrate to this path.

The changelog
(<https://docs.kalshi.com/changelog>, fetched 1 October 2026) subsequently
scheduled the legacy mutation routes to start returning "please switch to
the V2 endpoints" between 18 and 25 June 2026, and raised their token cost
to 10× the V2 cost before that. Affected mutations: create, cancel,
decrease, amend, and both batch routes under `/portfolio/orders`.

V2 shape, which `execution.order_request.to_event_order_v2` produces:

* path `POST /portfolio/events/orders` (relative to the trade-api v2 host)
* `side` is `bid` or `ask` on the **YES** book. A NO buy at `q` cents is
  `side=ask`, `price=(100−q)/100`. This is the mapping the handoff warned
  not to guess; it is the BookSide description on the create page.
* `count` and `price` are fixed-point strings (`"10.00"`, `"0.5600"`)
* `time_in_force` is required: `fill_or_kill`, `good_till_canceled`,
  `immediate_or_cancel`. `GTT` is not a value. An expiring order is
  `good_till_canceled` plus `expiration_time`.
* `self_trade_prevention_type` is required. We send `taker_at_cross`
  (cancel our incoming order rather than pull the resting quote).
* `post_only: true` is still set. A Kalshi demo wire on 30 September 2026
  returned `post only cross` for a crossing order
  (`docs/venue_evidence/kalshi_post_only_demo_20260930.md`). Acknowledging
  that evidence sets only `KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED`, and
  only when the caller passes the demo phrase. The flag defaults False,
  and it does not leave paper mode.
* cancel is `DELETE /portfolio/events/orders/{order_id}` and needs
  `market_ticker` in the query so the exchange can route the shard
  (<https://docs.kalshi.com/api-reference/orders/cancel-order-v2>)
* amend `count` is filled plus desired remaining, not remaining alone

`execution/quote_manager.py` and `venue/kalshi.py` post and cancel on these
paths. A single resting order is decreased or amended in place. Two orders
on one side still cancel-then-place, and a failed cancel still does not
place a third. Reads of `GET /portfolio/orders` for the resting snapshot
are unchanged; that route was not in the mutation-deprecation list.

PM US maker orders set `participateDontInitiate: true` on create and on
modify
(<https://docs.polymarket.us/api-reference/orders/modify-order>). The
racy local "virtual book" check is not a substitute. The local non-crossing
preflight in `execution/order_request.py` remains a preflight.

## 10. What is implemented in this branch, and what is not

Implemented, paper by default, live writes blocked:

* venue adapters for Kalshi (V2) and PM US, ForecastEx stub
* order state machine and reconcile-to-venue
* amend-first diff, wired into `QuoteManager` for the one-order case
* external-reference fair value and a reservation price that moves both
  sides' prices; the runner applies it when a reference is registered
* risk engine: market / series / underlying / venue / gross, daily-loss
  kill, disconnect and exit kills, order-group limit helper, the 30
  fills/minute halt on the shared clock
* paper hedge decisions with an equivalence allow-list and a log line
* pool ranking by net expected yield
* one capital figure
* estimated vs paid rewards, Kalshi per-series maker coefficients including
  combo, PM US rebate and taker fee with the published rounding example
* JSONL recorder, replay through `PaperFillSimulator`, daily report
* Kalshi selector (`mm/selector.py`): July 30 2026 snapshot score, $1
  market-period floor rounded down to the cent, greedy marginal $/day per
  dollar, hysteresis, competition and toxicity exits, shard-funding report
  with no collateral transfer. `tools/pool_report.py --demo` prints the table.
  April and May 2026 payouts are not a calibration input.
* Quote manager live place checks `require_live_execution_allowed(venue="kalshi")`.
  `enable_kalshi_maker_only_enforcement` arms that check only.
* Disconnect path: FIX logon tag 8013 behind a default-off flag (no socket),
  `SafeSender` order groups on the market's shard, `DeadMan`, and
  `python -m mm.safety.supervisor` which logs `cancel_all` and does not send.
* Unattended paper/demo loop (`docs/UNATTENDED.md`, `deploy/lip-unattended.service`,
  `Dockerfile`). Startup cancels before quoting. Websocket reference moves
  of one tick requote inline; REST is the fallback when the socket is stale.
  `optimize_sizes` sizes at the LIP reference. 15-minute programs stay in a
  bucket that is off unless asked. A 24h reward-to-markout ratio under 1
  cancels and latches.
* Compounding ledger, fractional-Kelly cap, drawdown throttle, and the
  $500→$10k ladder (`mm/compound.py`). PM US shared-pool divisor and paper
  `engine_place` (`docs/CROSS_VENUE.md`). One cross-venue capital cap, event
  net, and kill. The selector ranks Kalshi and PM US by net $/day per $.

Not done, and required before any of the $500–$1,000 live checklist:

* `KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED` and `MAKER_ONLY_ENFORCEMENT_VERIFIED`
  both default False. `LIVE_ARMED` is still required to construct a live
  quote manager. Production hosts still need `allow_production`.
* a cached `GET /series` table so `SERIES_FEES_ENABLED` can default on
  without an HTTP fetch inside the quote loop (the resolver still defaults
  off for that reason)
* a markout model trained on recorded fills. The selector uses category
  priors until `empirical_n` is at least 5.
* an equivalence table filled from rule text, per pair
* the supervisor does not hold keys. Pointing it at `SafeSender.trigger_all`
  is a deployment step, not the default.
* collateral is not moved between shards. The selector only reports the move.
* Polymarket US pool selection, including the shared reward-pool divisor.
  `harness/run_mm_paper.py` is not in this repo. Existing PM scoring is unchanged.
* ForecastEx, after member access and the retainer exhibit
* full-period replay against a Kalshi payout export. The replay reproduces
  the paper fill model. It does not reproduce a historical account.
