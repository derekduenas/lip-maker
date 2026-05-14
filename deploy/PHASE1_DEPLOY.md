# Phase 1 / Phase 2 deploy guide — paper data collection

After merging `claude/fix-lip-adverse-selection-gl6nt` (or pulling on the
prod DigitalOcean box), run these steps in order.

## 1. Pull and initialize

```bash
cd /root/lip-maker
git fetch origin
git checkout claude/fix-lip-adverse-selection-gl6nt   # or main after merge
git pull
python tools/init_phase1_schemas.py
```

Expected output: 6 ✓ lines for new modules + tables.

## 2. Confirm feature flags

All defensive defaults — nothing changes behavior until you flip these:

| flag | default | turn on when |
|---|---|---|
| `USE_MICROPRICE` | **true** | already on; safe additive |
| `AS_RESERVATION_ENABLED` | false | after 7d of markout data shows direction-bias bleed |
| `GRADUATED_THROTTLE` | false | after vpin_gate has written ≥50 throttle rows in paper |
| `PER_MARKET_CALIB_ENABLED` | false | after ≥5 settlements per series have updated calibration |
| `AUTO_HEDGE_ENABLED` | false | after `tools/hedge_effectiveness.py` shows GOOD verdict ≥14d |
| `AUTO_HEDGE_CME` | false | after AUTO_HEDGE_ENABLED + IBKR adapter validated with test order |
| `AUTO_HEDGE_KRAKEN` | false | when B.4 ships |

Override via env in the `lip-maker.service` file (`Environment=USE_MICROPRICE=true` etc.) or shell.

## 3. Restart paper runners

```bash
sudo systemctl restart lip-maker.service polymarket-maker.service
journalctl -fu lip-maker.service -n 50
```

Look for `microprice` lines on book updates and confirm no ImportError.

## 4. Install new periodic timers

```bash
sudo cp deploy/markout-backfill.{service,timer}     /etc/systemd/system/
sudo cp deploy/hedge-effectiveness.{service,timer}  /etc/systemd/system/
sudo cp deploy/go-live-check.{service,timer}        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
    markout-backfill.timer \
    hedge-effectiveness.timer \
    go-live-check.timer
systemctl list-timers --all | grep -E 'markout|hedge|go-live'
```

Cadence: markout backfill every 5 min; hedge effectiveness 03:00 UTC daily;
go-live gate 04:00 UTC daily.

## 5. What data starts flowing immediately (paper mode)

- `book_microprice_history` — one row per active ticker every ~30s, from
  the run_paper heartbeat. Builds the reference for any future markout.
  Sanity check (after 5 min uptime):
  ```bash
  python -c "import sqlite3; \
    n = sqlite3.connect('data/lip_maker.db').execute( \
      \"SELECT count(*) FROM book_microprice_history\").fetchone()[0]; \
    print(f'snapshots: {n}')"
  ```
- `lip_snapshots` — existing infrastructure, continues populating.

## 6. What data needs LIVE mode to populate

These tables stay empty in paper because there are no real fills:

- `fill_markouts` — needs fills_sync.py ingesting real /portfolio/fills
- `hedge_log` — same trigger path
- `broker_dry_run_log` — fires when AUTO_HEDGE_ENABLED with adapter in
  dry_run; without enabling, hedger never calls the adapter
- `market_throttle` (vpin source) — vpin_gate reads our own fill_ledger,
  so paper = no rows. Live = rows when our fills arrive
- `market_calibration` — needs paired (predicted, actual) settlement
  observations

For paper-mode data flow on the toxicity/hedge stack, you'd need to:
- Flip `LIP_PAPER=false` AND deposit at least a small live balance, OR
- Wait for the next live re-entry (gated by `go_live_check.py`)

## 7. Health-monitoring queries

```bash
# Microprice rate per active ticker (last hour)
python -c "import sqlite3; \
  c = sqlite3.connect('data/lip_maker.db'); \
  rows = c.execute('SELECT ticker, count(*) FROM book_microprice_history \
                    WHERE captured_ts >= strftime(\"%s\",\"now\") - 3600 \
                    GROUP BY ticker ORDER BY 2 DESC LIMIT 10').fetchall(); \
  [print(f'{r[0]:40s} {r[1]:>5}') for r in rows]"

# Daily markout report (will say "no fill_markouts" until live)
python tools/markout_report.py --since 24h

# Hedge effectiveness (will say "no paired data" until live + AUTO_HEDGE_ENABLED)
python tools/hedge_effectiveness.py --days 7

# Phase 3 gate
python tools/go_live_check.py --days 14
# exit 2 = insufficient data (expected until ≥14d live)
# exit 1 = some gate failed; check journal for details
# exit 0 = safe to flip live
```

## 8. Re-going-live procedure (when go_live_check exits 0)

1. Backup db: `cp data/lip_maker.db data/lip_maker.$(date +%Y%m%d).db`
2. Set the live caps via env in lip-maker.service:
   ```
   Environment=LIP_PAPER=false
   Environment=LIP_RAMP_PHASE=1
   ```
3. Verify the ramp_controller will start at 30% of paper cap (see
   `monitor/ramp_controller.py:228`).
4. `sudo systemctl restart lip-maker.service`
5. Watch `journalctl -fu lip-maker.service` for `[LIVE] PLACED` lines
   and immediate `daily_pnl` numbers.
6. After 24h of positive net PnL, flip `AUTO_HEDGE_ENABLED=true` to
   start placing hedges (still dry_run until `AUTO_HEDGE_CME=true`).
7. With IBKR Gateway running on port 7497 and a successful manual test
   order, flip `AUTO_HEDGE_CME=true`.

## 9. Rollback

Each commit is a single concern:

```bash
git revert <sha>   # for any individual phase concern
# or full rollback to pre-rebuild:
git checkout main && sudo systemctl restart lip-maker.service
```
