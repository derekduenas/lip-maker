# Claude Code handoff — Kalshi LIP maker
Date: 2026-09-21. This is the Kalshi build, not ATLAS or APEX.

## Goal and boundaries
Build an autonomous, inventory-aware maker whose net trading results plus paid incentives exceed fees and losses. Evaluate one shared $5,000 paper account; $40 was only a test seed. $1,000/day is an aspiration, not a forecast or acceptance criterion. Preserve paper-only interlocks. No live orders, deployment, new spending, or changes to ATLAS/APEX in this handoff. Continue reversible implementation, testing and commits autonomously.

Repo: derekduenas/lip-maker.
Remote working branch: claude/fix-lip-adverse-selection-gl6nt.
Last implementation commit on remote: f544b3632109196e50dc0577c3e464ebc45c317c.
Local equivalent: 8b6dd89, branch feat/maker-profit-research.
Implementation tree: 61273d4394a8b8ec4257d0936fa34b87477b6615.
The differing local/remote commit hashes resulted from publishing via GitHub tree/commit API. Fetch the remote branch; do not reset main or overwrite other work.

## Assessment
Useful book/order safety infrastructure and research components exist. This is not yet one commissioned, profitable system. Biggest gaps: integration, independent accounting, calibrated fill-loss economics, and complete-period evidence. No positive net edge has been established. Do not describe test counts as profitability evidence.

### Existing operating components
- execution/kalshi_ws.py: fixed-point book parsing, strict subscription sequences, stale/disconnect handling. Sub-cent prices excluded.
- execution/quote_manager.py: actual remaining quantity, fill identities, locks, resync, tombstones, owned-order handling, maker flag on direct path.
- run_paper.py: exposure checks before throttle, heartbeat, discovery freshness/expiry, actual-resting-order scoring and forward accrual.
- engine/lip_discovery.py: complete scans, exact windows and retirement safeguards.
- engine/lip_scorer.py: reference at target/5, full weight at or better than reference, target cutoff.
These descriptions are prior work, not a fresh proof of correctness. Re-audit race handling, fill ledger ownership, unknown execution responses and cancellation acknowledgements.

### Research components (not all wired into operating loop)
- research/profit_ledger.py: Decimal events, paid-vs-estimated separation, executable liquidation accounting.
- research/reward_reconciliation.py: explicit statement mapping and control-total validation.
- research/market_evidence.py: episode-aware markouts and evidence gates.
- research/reward_optimizer.py: candidate net-economic ranking with explicit forecast inputs. Forecasts are not calibrated.
- research/compound_capital.py: offline capital accounting; no complete venue reconciliation/order reservations.
- research/venue_capture.py: bounded authenticated WS capture plus manifests; gaps stop capture. No durable reconnect/lifecycle completeness.
- research/websocket_paper.py: capture THEN replay, not continuous online paper execution. Requires explicit fee/latency assumptions; market-open state assumed.
- research/maker_replay.py: latency/queue-aware hypothetical fills, reward accrual, inventory constraints; policies include join_best, defensive_maker, reward_depth, reward_inventory.
- reward_depth searches 16 price pairs from best through 3 ticks deeper, with fixed experiment size; optimizes snapshot share/funded capital, not fully calibrated net profit.
- reward_inventory suppresses heavy-side buying and caps light-side bids using worst unmatched entry cost, both fees and minimum pair margin. Passive pairing only; no deadline/aggressive unwind.
- Complementary matched inventory has a separate terminal-payoff valuation. It is not spendable cash, and funding/time costs remain absent.
- research/program_experiment.py compares alternatives each with its own budget. full_program_validated=false and selected_policy=None. It is not a shared-account portfolio test.
- research/profitability.py contains earlier holdout/scenario logic; do not assume newer policies participate.

## Verified issues from latest code/doc comparison
1. Critical accounting: tools/settlement_reconciler.py calls _estimate_rebate, writes it into rebate_earned_usd and net_outcome_usd, then calls calibration_ewma.update(actual_usd=rebate). This is estimated data labeled actual and circular calibration. Separate columns/provenance, migrate or quarantine contaminated history, and allow only independently reconciled payments into payout calibration.
2. Execution inconsistency: venue/kalshi.py claims post_only does not exist and substitutes no_self_trade. Self-trade prevention is not maker protection. Direct quote_manager path does send post_only=True. Consolidate adapters and verify request/response contracts.
3. Current official docs recommend V2 /portfolio/events/orders with bid/ask and fixed-point prices/counts. Existing code posts legacy /portfolio/orders. Verify compatibility and migrate deliberately; do not simply rename fields without testing NO-to-ask semantics and fill accounting.
4. New strategy/ledger/compounding pieces are not integrated into one continuous runner and shared capital allocator.
5. Fees in experiments are assumptions, not verified market-specific schedules. Implement schedule/override/rounding handling and retain provenance.
6. Replay lacks reliable lifecycle, learned conditional adverse-selection costs and complete-period/payment validation.
7. Passive inventory pairing can remain stuck. Add maximum holding time and a cost-aware exit policy; distinguish exposure reduction from opening more risk.
8. Program periods may overlap. Verify program-ID-level state, attribution and caps; ticker-only state must not silently overwrite simultaneous programs.
9. Sub-cent support remains excluded. Do not claim all markets supported.
10. program_experiment limitations text still says active inventory hedging/unwinding is absent; clarify passive pairing exists while active exit remains absent.

## Current official references and interpretation
- https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program
  Random snapshot each second; reference cumulative target/5; cutoff target; at/better reference weight 1, below weight DF^ticks. Whole book must meet target on both sides. Individual participant need not personally quote both sides. Normalize by side; payout incorporates excluded snapshots, cent floor and $1 minimum. Current governing notices remain authoritative; do not claim help-page review is legal certification.
- https://docs.kalshi.com/api-reference/incentive-programs/get-incentives
- https://docs.kalshi.com/openapi.yaml
  Earlier schema inspection found pool/cap values in centicents, divide by 10,000 USD. Reverify before implementing a schema change.
- https://docs.kalshi.com/api-reference/orders/create-order-v2
- https://docs.kalshi.com/api-reference/orders/get-order-queue-position
- https://docs.kalshi.com/getting_started/order_groups
- https://docs.kalshi.com/getting_started/fee_rounding
- https://docs.kalshi.com/getting_started/market_lifecycle
- https://docs.kalshi.com/llms.txt
- https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf
  Inventory-sensitive valuation and execution probability are useful principles; stock diffusion assumptions do not establish an optimal Kalshi sports strategy.

## Evidence and limits
Committed docs/REWARD_DEPTH_EXPERIMENT.json contains a short development replay. Copper ~187 seconds: size10 join_best -21.6462 before rewards, reward_depth -4.6103; estimated rewards .12357 and .08928. Size50 join_best -89.7429, reward_depth -3.5688; estimates .59083 and .18601. These use immediate liquidation, assumed costs, and inspected development data. They neither establish long-run losses nor profitability, and are not a fair empirical test of the later paired-hold policy.
No real-tape result yet establishes reward_inventory edge.
docs/REWARD_40_DOLLAR_PROBE.json static REST observations were stale for execution gates. Projected payout is not earned money.
Latest full suite before subsequent patches: 393 passed plus 2 subtests. Latest targeted suite after pairing: 27 passed. Run the current full tests directory; do not report the old count as current.

## Data and access boundaries
Historical reward receipts are not proof of net trading profit. Match independently supplied private receipts to fills, fees and capital usage when available. Do not commit account records or credentials to this public repository.
Raw research captures may not be available in a new checkout. Use committed experiment summaries as development evidence only; request secure data provisioning if needed and continue authorized offline work meanwhile.

## Next work, in order
1. Independently inspect code and write docs/CLAUDE_INDEPENDENT_ASSESSMENT.md: verified/contradicted/unverified findings, severity, evidence, missing end-to-end behavior and prioritized work. Do not accept this handoff's claims on faith.
2. Fix accounting provenance and maker adapter inconsistency; audit schema migration and contaminated calibration recovery. Preserve paid/estimated distinctions everywhere.
3. Build one continuous paper loop from existing components: event-driven book/lifecycle/order/fill state, versioned program metadata, one shared $5,000 cash/reservation ledger, economic quote selection and inventory control. Replay and paper must use the same strategy decisions and accounting.
4. Add market-specific fees, conditional fill/markout estimates, inventory age/exit rules and exchange-native safeguards. Treat forecast inputs as unknown until calibrated.
5. Capture complete incentive periods when credentials/runtime permit; retain raw events privately and test reconnects. Do not fake continuous operation from a short capture or launch a background service without an approved host.
6. Evaluate frozen policies on later independent periods with shared capital, inventory outcomes, fees, payout reconciliation, drawdown, concentration, downtime and uncertainty. Include doing nothing and simple maker baselines. Preserve unfavorable results.
7. Report exactly what ran, what was proven, what remains blocked, commit hashes and next actions. No live promotion without explicit authorization and credible evidence.

## Validation workflow
Read AGENTS.md and relevant repo instructions first. Inspect git status and preserve others' changes. Use python -m pytest tests -q in a dependency-equipped environment; unrestricted pytest can collect unrelated scripts. Add meaningful regression tests for money/state failures, then a full gate after integration. Do not spend successive turns only adding synthetic tests. Publish coherent commits on the working branch without force-pushing. User wants continued implementation, not another plan-only handoff.
