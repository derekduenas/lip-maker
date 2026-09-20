# Fill reconciliation repair

Baseline: `64f45d9`. This patch changes order-state reconciliation and fill
consumer recovery. It does not enable live trading or establish profitable
reward economics.

## Order state

Live executions are durably ingested and invalidate local order quantities.
They do not subtract a delta from a REST quantity whose included executions
are unknown. The runner breaks reward accrual and requests REST reconciliation.
Until reconciliation succeeds, affected orders cannot be scored and new
quoting is blocked. Exposure gates pull orders when state remains uncertain.

REST requests capture a local mutation generation before fetching. A response
that races a fill, placement, cancellation or another accepted reconciliation
is rejected. A fresh uncontended response replaces quantities in either
direction, allowing correction of historical undercounts. This relies on the
venue's complete REST response being authoritative; it is not an assertion
that REST and private feeds share an execution watermark. Paper fills continue
to update simulated quantities locally.

Ledger ingestion is distinct from in-process handling. REST-first ingestion
does not suppress WS-driven reconciliation. Restart replays invalidate local
state again without adding another ledger row. Failed persistence is not
remembered as successful processing and keeps quoting blocked. A retry of
that execution can clear its persistence failure; an unrelated successful
fill cannot. Missing execution identities are quarantined rather than guessed.

## Consumers

`tools/fill_consumers.py` drains durable rows independently of their source.
`fill_consumer_receipts` tracks quote, markout, and hedge-diagnostic completion
separately. `fill_consumer_attempts` rotates incomplete work through bounded
batches. The existing fills-sync poller drains old rows even when the current
API response contains no new fills. The runner also drains the observed fill.

- Quote totals are recomputed from the ledger. Update and completion receipt
  commit atomically. Partial fills do not falsely mark the entire quote filled.
- Missing quote rows or markout history remain pending for retry.
- Diagnostic sinks use their existing unique fill keys. A crash after the
  effect but before the receipt is safe to replay.
- Hedge replay calls only `decide` and `persist`, never the executable hedge
  path. Automated hedge execution needs its own durable execution design.
- Inventory aggregation in the REST poller retains fractional quantities.
- NO fills with only the documented YES-price field derive the complementary
  NO cost instead of losing their execution price.

No deployment or venue writes were performed. Existing paper/live interlocks
are unchanged. Program terms, reward caps, heuristic ranking and the existing
fair-value policy disagreement remain outside this patch.

## Verification

The full suite run reported 319 passes and the two previously known
`test_fair_value_gate` failures. Four additional recovery/parser tests were
then added; the final focused suite of 179 tests passed. `git diff --check`
passed. Coverage includes both REST/WS orderings, restart after ingestion,
concurrent duplicate notifications, old snapshots versus new placements,
failed ingestion, transactional rollback, diagnostic replay, fractional
inventory and quote-row arrival after a fill.
