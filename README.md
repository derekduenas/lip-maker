# INNAIT — Cross-Venue Maker

Single-organism market-making system across **Kalshi** (LIP) and **Polymarket US** (LIP).
Goal: $20k/mo recurring rebate-driven yield by Sept 1, 2026.

## Layout

```
lip-maker/
├── kalshi/  ← (legacy: top-level files are Kalshi)
│   ├── engine/         lip_discovery, lip_scorer, futures_feed, yield_equation
│   ├── execution/      kalshi_auth, kalshi_ws, quote_manager, market_blacklist
│   ├── monitor/        alerts, ramp_controller, reconciliation
│   ├── tools/          dashboard, settlement_reconciler, bleed_monitor,
│   │                   share_alert, strategy_outcome_check, fills_sync,
│   │                   competitor_density, vip_tracker, lip_sunset_alarm, ...
│   └── run_paper.py    main runner
│
├── polymarket/         ← Polymarket US maker (mirrors Kalshi structure)
│   ├── engine/         pm_scorer, rewards_schedule, pm_fair_value, yield_equation
│   ├── execution/      pm_auth, pm_ws, pm_quote_manager
│   ├── tools/          us_scanner, pm_dashboard, pm_bleed_monitor,
│   │                   pm_settlement_watcher, pm_rebate_verifier, public_recorder
│   └── run_pm.py       main runner
│
├── cross_venue/        ← Tools that span both venues
│   ├── orchestrator.py        RYD per venue, capital migration recommend
│   ├── capital_reaper.py      stale-order canceller, both venues
│   ├── capital_injection.py   DCA allocator (split funds by trailing RYD)
│   ├── innait_status.py       combined dashboard (one command, both venues)
│   ├── fill_rate_tracker.py   per-series quoted vs filled
│   ├── yield_equation.py      unified physics (deployed dual-side too)
│   └── db_backup.sh           nightly snapshot, both DBs
│
├── _archive/2026-04-29/  ← dead code (kept for reference)
└── data/                 ← Kalshi SQLite (lip_maker.db)
```

## Production deployment

Both runners deployed as systemd services on DigitalOcean:
- `lip-maker.service` → `python run_paper.py`
- `polymarket-maker.service` → `python run_pm.py`

10 cron jobs: capital_reaper (5m), orchestrator (5m), bleed_monitor ×2 (10m),
fill_rate_tracker (30m), pm_settlement_watcher (30m), share_alert (1h),
pm_rebate_verifier (01:05), strategy_outcome_check (01:15),
db_backup (02:30), lip_sunset_alarm (09:00).

## Unified Yield Equation (engine/yield_equation.py)

```
ExpectedDailyRebate = pool_per_day
                    × our_share
                    × qualify_prob       (smooth sigmoid around target_size)
                    × time_factor        (exp(-d/90) — long markets dilute)
                    × calibration        (theoretical → actual, ~0.25 Kalshi, ~0.10 PM)
                    - adverse_cost       (0.5%/d × position × sqrt(t))
```

Used by both venues' scorers as primary ranking signal. `.explain()` method
exposes every component for audit.

## Key constraints discovered

- **PM US has no `/v1/incentives` API** as of 2026-04-30 (404 with valid auth).
  Hardcoded SCHEDULES from docs is the only ground truth.
- **PM payouts**: 7-12 calendar days post-period-end (not daily).
- **Snapshot share is the bottleneck**: at $260 PM bankroll, our share is
  structurally ~0% in target_size≥1000 markets. Capital injection or
  ultra-concentration required.

## Sept 1 LIP sunset

Kalshi's Liquidity Incentive Program ends Sept 1, 2026 (~123 days).
`tools/lip_sunset_alarm.py` fires daily warnings at T-60/30/14/7/3/1.
