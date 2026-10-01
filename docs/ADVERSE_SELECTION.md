# In-loop adverse-selection guard (2026-09-30)

Paper mode stays the default (`LIP_PAPER=true`, `PM_PAPER=true`) and live
transmission is still blocked by `execution/order_request.py`
(`MAKER_ONLY_ENFORCEMENT_VERIFIED=False`). Every switch below only removes or
widens quotes, so it is ON by default.

| Flag (env) | Default | Effect |
|---|---|---|
| `AS_GUARD_ENABLED` | true | Post-fill fade, fill-burst pull, markout-toxicity widen/pull |
| `PULL_ON_VOLATILITY` | true | A volatile book **cancels** quotes and starts a cooldown. Before this change the `volatility` skip was transient: it stopped repricing and left stale quotes resting |
| `INVENTORY_SIDE_CAP_ENABLED` | true | At the net-inventory cap the heavy side is removed and the reducing side keeps quoting. In the soft zone (`INVENTORY_SOFT_FRACTION`) the heavy side backs off one tick if it still scores; otherwise it falls back to size skew. Before this change, hitting the cap refused the whole target and left the heavy-side order resting |
| `SERIES_FEES_ENABLED` | **false** | Per-series Kalshi maker fees from `/series` `fee_type`. This is less conservative than the global schedule, so compare in paper before turning it on |

## Rules (engine/adverse_selection.py)
1. **Post-fill fade**: after a fill on side S, S is not quoted for `AS_FILL_COOLDOWN_SEC` (15 s).
2. **Burst**: same-side fills of at least `AS_BURST_CONTRACTS` (100) within `AS_BURST_WINDOW_SEC` (60 s) pull both sides for `AS_BURST_COOLDOWN_SEC` (120 s).
3. **Markout toxicity**: each fill is marked against the YES mid at 5, 30 and 120 s. A quantity-weighted EWMA of the 30 s markout is kept per market. Once it has at least `AS_MIN_OBS` (3) observations:
   - at or below −`AS_WIDEN_MARKOUT_CENTS` (1c): back off one tick;
   - at or below −`AS_PULL_MARKOUT_CENTS` (3c): pull for `AS_TOXIC_COOLDOWN_SEC` (600 s).
4. **Volatility pull**: pull for `AS_VOLATILITY_COOLDOWN_SEC` (30 s).

Pulls bypass the 30 s skip-cancel throttle (`FORCE_PULL_REASONS`).

## Refit the thresholds from data
Every matured markout is written to `as_markouts` (ticker, side, fill_ts,
price, qty, horizon, mid, markout). After about 2 weeks of paper fills:
```sql
SELECT substr(ticker,1,instr(ticker,'-')-1) AS series, horizon_sec,
       COUNT(*) n, AVG(markout_cents) avg_mo, SUM(markout_cents*qty) pnl_cents
FROM as_markouts GROUP BY series, horizon_sec ORDER BY pnl_cents;
```
Set the thresholds so that the reward each fill earns, net of fees, exceeds the
expected markout loss. The defaults are starting points, not fitted values.

## Known limitations
- Paper accrual still requires two-sided resting orders (`_actually_resting`).
  Kalshi pays one-sided participants, so one-sided periods are under-credited.
  This biases paper results conservatively.
- The guard only sees the venue's own book. It has no external spot or news
  feed yet; see the audit report for the design.
