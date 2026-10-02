# Archive 2026-10-01 — review cleanup

Moved with `git mv` (history kept), out of the import path. Nothing here is
import-reachable from the APEX entrypoints (`python -m mm.unattended`,
`python -m mm.safety.lip_watchdog`, `python -m mm.replay`); checked with an
AST import graph before each move. Paths under this folder mirror the
original repo paths.

## Live-capable legacy tools with no interlock
- `tools/scale_caps.py` — rewrote the lip-maker unit, defaulted to `LIP_PAPER=false`.
- `tools/go_live.py` — `--force` bypassed the readiness gates; share gates broken.
- `tools/stranded_liquidator.py`, `stranded_liquidator.py` — diverged copies;
  `--live` posted real exit orders without an interlock and could cross with post_only.
- `tools/edge_hunter.py` — LLM loop that auto-cancelled live orders.
- `tools/depth_recheck.py` — cancelled live orders by default.
- `tools/install_creds_safe.py` — credential installer for the retired root unit.
- `cross_venue/hedger.py`, `cross_venue/hedge_unwind.py` — unwound dry-run hedges
  with real orders; the PM hedge leg was always a placeholder slug.
  Callers fixed: `tools/fill_consumers.py` (hedge_diagnostic consumer removed),
  `tools/settlement_reconciler.py` (unwind hook removed), `tools/init_phase1_schemas.py`.
- `tools/hedge_effectiveness.py` — only consumer of `hedge_log`; also mixed price
  units. `tools/go_live_check.py`'s basis gate now reports insufficient data.
- `monitor/ramp_controller.py` — rewrote `/etc/systemd/system/lip-maker.service`
  and restarted it.

## Stale systemd units
Every unit/timer that targeted `/root/lip-maker` or `/root/polymarket-maker`
(the pre-APEX root install): `deploy/lip-maker.service` (set `LIP_PAPER=false`,
ran as root, wrote the shared heartbeat), the `deploy/*.timer` + `.service`
pairs (arb-scan, blocklist-review, dead-slot-pruner, go-live-check,
hedge-effectiveness, lip-state-hygiene, markout-backfill, order-flow-tracker,
unrealized-pnl), root `series-auto-prune.*`, `vpin-gate.*`, and
`polymarket/deploy/*`. The only units kept are `deploy/lip-unattended.service`,
`deploy/apex/lip-watchdog.service` and the drop-ins in `deploy/apex/`.

## Decorative / dead code
- `agents/` — 14 agent skeletons that only raised NotImplementedError; no importers.
- `venue/` — adapter layer imported only by its own tests (those tests were
  removed from `tests/test_order_request.py`); `mm/venues/` is the live abstraction.
- `dislocation/` + `tools/dislocation_{backtest,parity_check,scan}.py` — self-contained,
  no importers outside itself.
- `engine/share_drift.py`, `engine/maker_rebate_scorer.py` — no importers anywhere.
- `config/macro_calendar.py` + `tools/macro_blackout_sync.py` — every date was before
  Oct 2026 and the June 2026 FOMC was wrong (real: Jun 16-17, 2026).
