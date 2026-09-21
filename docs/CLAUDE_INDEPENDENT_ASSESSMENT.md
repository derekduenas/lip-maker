# Independent assessment — Kalshi LIP maker

Date: 2026-09-21. Assessed tree: `afe915f10bfc79ad4500f5747c5133af242ae41c`
(branch `claude/fix-lip-adverse-selection-gl6nt`). This assessment was written
by inspecting code, not by accepting `docs/CLAUDE_CODE_HANDOFF.md`.

Scope note: this is the Kalshi build. No ATLAS/APEX files were read or changed.

## 0. Method and what I could not do

Verified by reading source and running the suite. **Full suite on this tree:
424 passed, 2 subtests, 0 failed** (`python -m pytest tests -q`).

One apparent failure (`test_signatures_match_documented_digest_length`) was an
environment defect, not a code defect: the sandbox's system `cryptography`
could not import `_cffi_backend`. Reinstalling `cffi`+`cryptography` made it
pass. No test change was made. Anyone seeing that failure should fix their
environment rather than the test.

**Evidence limitation that blocks a named priority.** `docs.kalshi.com` and
`help.kalshi.com` are denied by this environment's egress proxy
(`gateway answered 403 to CONNECT`, confirmed for both hosts via
`$HTTPS_PROXY/__agentproxy/status`). I therefore **could not verify any claim
about current official Kalshi API or LIP documentation**, including:

- whether V2 `/portfolio/events/orders` exists or is recommended over
  `/portfolio/orders`;
- whether a literal `post_only` flag exists on limit orders;
- the current LIP reference/cutoff definition;
- fee schedules and rounding;
- centicent units on incentive endpoints.

Every statement below about Kalshi's API or LIP rules is therefore **either a
statement about what this repository's code does, or is marked UNVERIFIABLE
HERE.** I did not treat the handoff's doc summaries as verification, and
neither should the next reader. This is the single largest gap in this
assessment and it is environmental, not analytical.

## 1. What works end to end

These are real and I confirmed them by reading the code paths, not by trusting
prior descriptions.

- **Book ingestion** (`execution/kalshi_ws.py`). Fixed-point v2 parsing,
  per-subscription sequence tracking with strict gap → `stale`, disconnect
  marking, explicit rejection (not rounding) of off-cent prices via
  `_price_to_cents` / `unsupported_grid`.
- **Order-state truth** (`execution/quote_manager.py`). Remaining quantity as
  float, fills idempotent by `trade_id` (`apply_fill`, `_fill_known`), all
  mutations under `_state_lock`, REST/WS ordering resolved by the
  monotonic-decrease rule in `_merge_live_orders`, tombstones preventing
  re-adoption.
- **Exposure gating** (`run_paper.py`). `_exposure_gate` runs before the
  scoring throttle; stale/expired/off-grid/blacklisted/unfresh-discovery all
  cancel. Heartbeat repeats the gates independently.
- **Forward reward accrual** (`run_paper.py` `AccrualState`, `_accrue`).
  Credits the previously observed share over the elapsed interval, breaks on
  unknown state, clips to the program window, caps cumulatively.
- **Discovery completeness** (`engine/lip_discovery.py` `discover_result`).
  Complete-vs-partial scans, demotion only on complete scans,
  `discovery_runs` freshness.

**What does not work end to end: the money.** Everything above is *state
correctness*. There is no continuous path from a quote decision to a cash
number. See §3.

## 2. Verified defects

### V1 — CRITICAL. Estimated rewards are written as actual and fed back into calibration.

Confirmed at `tools/settlement_reconciler.py`:

- `:363` `rebate = _estimate_rebate(conn, tkr)` — a model output
  (`:129` `sum_share * period_reward / period_seconds * SNAPSHOT_REPRESENTS_SECONDS`).
- `:371-378` writes it into `settlement_log.rebate_earned_usd` and
  `net_outcome_usd` — columns whose names assert realized fact.
- `:399-405` `calibration_ewma.update(predicted_usd=pool_per_day,
  actual_usd=rebate)` with the inline comment `# what we actually earned`.

This is circular: the estimate is produced by our own snapshot model, then
used as ground truth to calibrate that same model. The loop cannot detect its
own error and will converge on self-consistency rather than accuracy.

`SNAPSHOT_REPRESENTS_SECONDS = 30.0` (`:70`) is itself a fitted fudge factor —
its own comment records per-series variation of 0.18–0.68, i.e. a ~3.8x spread
that the single constant flattens.

**Blast radius** — the contaminated calibration drives real decisions:
`engine/capital_allocator.py:352` (`kalshi_calib_for` → sizing) and
`tools/attack_targets.py:186` (`calib_for` → ranking). So a fabricated
"actual" changes which markets are chosen and how much capital they get.

Severity: critical. This is the defect most likely to produce a confident
false profitability claim. I agree with the handoff's ranking.

### V2 — HIGH, but latent rather than live. Two order adapters disagree on maker protection.

`venue/kalshi.py:121-126` asserts `# Kalshi's API doesn't have a literal
post_only flag` and substitutes `body["no_self_trade"] = True`.
`execution/quote_manager.py:842` sends `"post_only": True`.

Two corrections to the handoff's framing:

1. **Self-trade prevention is not maker protection** — the handoff is right,
   and this matters: `no_self_trade` only stops you trading against your own
   resting order. It does nothing to stop your buy crossing someone else's
   ask and paying taker fees. A "maker" strategy running through that adapter
   could silently take liquidity.
2. **But `venue/kalshi.py` is unreachable from the runner.** I grepped for
   `KalshiVenue` and `from venue` across the repo: no instantiation outside
   `venue/` itself. `run_paper.py` imports only `engine.*`, `execution.*`,
   `cross_venue.*`, `monitor.*`, `tools.fill_consumers`. So this is **dead
   code today**, and severity is "trap waiting for the next integrator", not
   "currently mis-sending orders". The handoff's wording implies active
   execution risk; I did not find that.

Which of the two flags is actually correct is **UNVERIFIABLE HERE** (docs
blocked). That uncertainty is itself the reason to consolidate to one path.

### V3 — HIGH. Three mutually inconsistent capital figures; no reservation concept.

- `config/settings.py:52` `BANKROLL_USD = float(os.getenv("LIP_BANKROLL", "80"))`
  — defaults to **$80**.
- `execution/quote_manager.py:556` `if self.paper: return 10_000.0`
  — paper risk math uses a hardcoded **$10,000**.
- The handoff and the stated goal specify a shared **$5,000** account.

Consequence: `MAX_BANKROLL_SHARE_PCT = 0.50` is enforced against $10,000 in
paper, i.e. it permits $5,000 of gross exposure — 100% of the intended
account, not 50%. The safety cap is inert in exactly the mode we are
evaluating in.

Separately, I searched `execution/`, `engine/`, `run_paper.py` for any
capital-reservation concept (`reserve`, `available_cash`, `free_cash`): there
is none. `engine/reservation_price.py` is Avellaneda-Stoikov pricing,
unrelated. Exposure is computed by summing resting orders
(`_total_gross_exposure`), which is not the same as knowing whether cash is
available to fund a new order.

### V4 — HIGH. The research stack is not connected to the runner at all.

`run_paper.py` imports **zero** `research/` modules, directly or
transitively. `research/__init__.py:1` says so itself: *"Offline maker
economics. No order submission capabilities."* Every research module is
reachable only from `tools/maker_research.py` (`__main__`),
`tools/reward_budget_probe.py` (`__main__`), or `tests/`.

There are two disjoint accounting worlds:

| | Operating loop | Research |
|---|---|---|
| Cash | none; `_get_balance` stub | `research/profit_ledger.py` `profit_events` |
| Capital base | `$10,000` hardcoded / `$80` setting | `$40` (`compound_capital.py:9`), `$10` (`maker_replay.py:17`) |
| DB | `data/lip_maker.db` | caller-supplied `--db` |

And the same concept is implemented **twice or three times**:

- **Scorer twice**: `engine/lip_scorer.py` `score_snapshot` vs
  `research/reward_optimizer.py:20-44` `_side`. Both implement the same
  reference-at-`target/5` model.
- **Capital allocation three ways**: `engine/capital_allocator.py`
  `select_optimal_portfolio`, `research/market_evidence.py:115-124`,
  `research/compound_capital.py:48`.
- **Fill model twice**: `execution/quote_manager.apply_fill` (real lifecycle)
  vs `research/maker_replay.py:40` `replay` (queue counterfactual).
- **Markouts twice at identical horizons**: `monitor/markout_logger.py`
  (microprice) vs `research/market_evidence.py:25` (depth-weighted exit).
- **Liquidation valuation twice**: `monitor/unrealized_pnl.py:89` vs
  `research/profit_ledger.py:126-155`.

This directly violates the stated requirement that *replay and paper use the
same strategy decisions and accounting*. Today they cannot: they are
different code implementing different models against different capital.

### V5 — HIGH. No inventory exit exists in the operating loop.

`run_paper.py` contains no `liquidat*`, `unwind`, `flatten`, or
`exit_position` logic. Inventory acquired by a fill is held until settlement.
Passive pairing exists only in `research/maker_replay.py` (`:275` ceiling,
`:341` `paired = min(positions.values())`), which the runner never calls.
There is no maximum holding age and no cost-aware exit anywhere.

The handoff's item 10 asks to clarify that passive pairing exists while active
exit does not. Stronger statement, verified: **in the operating loop neither
exists.**

### V6 — MEDIUM. Ticker-keyed state can conflate overlapping programs.

`init_db.py:21` makes `lip_programs.id` the primary key with
`UNIQUE(market_ticker, start_date)` — so two programs on one ticker with
different starts *can* coexist as rows. But:

- `run_paper.py` `params_by_ticker` and `_accrual` are keyed by **ticker**, so
  only one program per ticker survives in memory;
- `AccrualState.program_key` is the window `start_ts`, which detects a change
  but cannot represent two simultaneous programs;
- `_seed_accrued` sums `lip_snapshots` by `market_ticker` only, so it would
  pool two programs' history.

Notably `research/profit_ledger.py` already requires `program_id` on every
event (`:32`) and groups by `(market, program_id)` (`:88`) — the research side
got this right and the operating side did not.

Severity medium: it requires genuinely overlapping programs to bite, which I
could not confirm happens in practice (UNVERIFIABLE HERE — needs live data).

### V7 — MEDIUM. Fee and cost inputs are assumptions carrying no provenance.

`research/maker_replay.py:21` `exit_fee_per_contract_usd: str = '0.02'`,
`:35` `min_pair_margin_usd: str = '0.01'`, plus a maker fee constant. These
are declared defaults with no cited schedule, no market-specific variation,
and no rounding rule. The operating loop has no fee model at all — it never
computes a fee, because it never computes cash.

Any net-profit number produced today is therefore a function of an
unsourced constant.

## 3. Missing components needed for profitable operation

Ordered by what blocks a *trustworthy* profit number, not by effort.

1. **A single account ledger.** One cash balance, per-order capital
   reservations taken at placement and released on cancel/fill, inventory
   valued separately from spendable cash. Without it "profit" has no
   denominator and the bankroll cap is decoration.
2. **Provenance-typed rewards.** `estimated` and `paid` must be different
   columns with different names, and only `paid` may ever reach calibration.
3. **One decision function** used by both the live paper loop and replay, so a
   replay result is evidence about the thing that actually runs.
4. **A fee module** with an explicit schedule, override, rounding and a
   recorded source, consumed by both paths.
5. **Inventory exit policy**: max holding age plus a cost-aware unwind that is
   only allowed to reduce exposure.
6. **Complete-period reconciliation** against independently supplied payment
   records, with control totals.

## 4. Claims I disagree with or cannot verify

**Disagree (severity/framing):**

- Handoff item 2 presents the adapter split as live "execution inconsistency".
  Verified: `venue/kalshi.py` is dead code w.r.t. the runner (§V2). Real, but
  latent.
- Handoff item 10 asks to clarify that passive pairing exists and active exit
  does not. In the *operating loop* neither exists (§V5). The clarification as
  worded would still overstate the runner's capability.

**Cannot verify here (egress blocked — not disputed, just unproven):**

- Handoff item 3 (V2 orders endpoint recommended; bid/ask + fixed point).
- The LIP mechanics summary in "Current official references" — including
  reference-at-`target/5`, which the code now implements
  (`engine/lip_scorer.py:155,162`) and which I cannot confirm against the help
  page. **If that rule is wrong, every share number in this system is wrong**,
  so it deserves priority when access exists.
- Centicent units on incentive endpoints.
- Any fee schedule.

**Cannot verify (no data):** every profitability figure. The committed
`docs/REWARD_DEPTH_EXPERIMENT.json` is a ~187-second development capture; the
handoff already says it establishes neither profit nor loss, and I agree. I
did not find any artifact in this tree that would support a profitability
claim, and I am not producing one.

**Confirmed accurate in the handoff:** items 1, 4, 5, 6, 7 (as applied to
research), 8, 9; the existing-components list; and the instruction not to
report test counts as profitability evidence.

## 5. Prioritized plan

**P1 — Reward provenance and calibration quarantine.** Split estimated vs
paid at the schema level; stop `_estimate_rebate` writing `rebate_earned_usd`;
admit only reconciled payments to `calibration_ewma`; quarantine existing
rows; point consumers at the right column. *Rationale: this is the defect that
manufactures false confidence, and everything downstream inherits it.*

**P2 — Consolidate order placement.** One maker-safe request builder; make the
divergent adapter fail loudly instead of silently substituting a different
flag; isolate the request contract so an API migration is a single edit when
docs are reachable.

**P3 — One shared $5,000 ledger with reservations.** Extend
`research/profit_ledger.py` (already Decimal, immutable, `program_id`-keyed,
with `reserve` and separate `reward_credit`/`reward_estimate` kinds) rather
than writing a third accounting system. Wire the runner to it; delete the
`$10,000` stub.

**P4 — Fees with provenance + inventory exit rules.** Single fee module used
by both paths; max holding age; cost-aware exit restricted to exposure
reduction.

**P5 — Program-identity keying.** Key runner state and accrual by
`(program_id)` rather than ticker.

**P6 — Complete-period validation.** Only meaningful with data access.

Deliberately *not* on this list: more synthetic tests as a substitute for
evidence, and any live promotion. Paper-only interlocks stay.
