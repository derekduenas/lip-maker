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
