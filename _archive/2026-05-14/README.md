# Archive 2026-05-14 — Phase 0 cleanup

Pre-rebuild dead-code archive. Nothing in this folder is imported by any
live module as of cleanup date (verified via grep of importers).

## Contents

### `2026-04-29/`
Prior archive folder, folded in.

### `engine_alpha/`
Was `engine/alpha/`. Scraper-based alpha stack (weather, music, HDD,
kworb). Never wired to production runners or systemd. Internal imports
only; no external users.

### `engine_sniper_select.py`
Was `engine/sniper_select.py`. Already commented out at
`run_paper.py:43` (`# archived 2026-04-29 (audit: unused)`).

### `tools_dead/`
14 tool scripts with zero importers, zero systemd timer references,
zero docs mentions: `zombie_liquidator`, `vip_tracker`,
`truth_reconciler`, `strategy_outcome_check`, `rebate_flow_alert`,
`profit_attribution`, `price_velocity_kill`, `pool_depletion`,
`pool_analysis`, `news_kill`, `missed_opportunity_scanner`,
`inventory_ghost_reaper`, `fill_velocity_kill`, `alpha_paper_trade`.

### `root_quote_manager.py`
Was `/quote_manager.py`. Canonical is `execution/quote_manager.py`,
which is what `run_paper.py:54` imports. Root copy was an older
snapshot — missing the 2026-05-01 inventory-cache-TTL fix and
2026-05-02 PREDATOR C2 cancel-race fix that live deploys to
`execution/quote_manager.py`. No bare `import quote_manager` callers
existed.

### `root_settings.py`
Was `/settings.py`. Canonical is `config/settings.py`, which every
live importer uses (`from config import settings`). Root copy has
unique 2026-05-09 "TIER1I" weather/macro/geopolitical caps in
`MAX_GROSS_PER_MARKET_BY_SERIES` that were **never deployed** because
nobody imports from root — but the file also misses crypto-MAX/MIN
boosts and bleed-bans that landed in `config/settings.py` 2026-05-02
through 2026-05-06. Keeping for forensic reference; cherry-pick the
TIER1I block manually if/when needed.

## Recovery

```bash
git mv _archive/2026-05-14/<thing> <original_path>
```
