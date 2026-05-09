# OPEN ITEMS — Niche LIP + Music Alpha Pivot (2026-05-02)

## Strategic question to validate first
**A/B test: niche LIP vs shotgun LIP**
- Week 1: restrict top_n_to_quote to music + weather + commodity series ONLY
- Measure yield per $ deployed vs prior 7d shotgun baseline
- Decision rule: if niche wins by >30%, convert permanently

## Music Alpha build (4-week sprint)

### Day 1 (DONE 2026-05-02)
- ✅ Pulled 441 LIP-eligible music markets from Kalshi
- ✅ Categorized by sub-niche: streaming(116), albums(84), awards(79)
- ✅ Identified top 3 modelable: KXRANKLISTSONGSPOTGLOBAL, KXALBUMEQUIV, KXAMA/CMA/ACM
- ✅ Saved to /tmp/kalshi_music_markets.json

### Day 2-7 (data + model)
- [ ] Spotify Web API integration (free, no auth for charts)
- [ ] Pull current bid/ask/depth on all 116 KXRANKLISTSONGSPOTGLOBAL markets
- [ ] Backfill 6mo Spotify rank history for top 30 artists
- [ ] Logistic regression: P(artist #1 by date X) given rank + 7d momentum + release calendar
- [ ] Luminate trial signup for Album Equivalent Units data
- [ ] Backtest against settled markets via lip_programs end_dates

### Day 8-14 (prototype)
- [ ] Fork engine/lip_scorer.py → engine/music_alpha_scorer.py
- [ ] Decision logic: |model_p − market_p| > 8% AND depth ≥ $200 AND position ≤ 5% bankroll
- [ ] Wire to existing quote_manager.py for execution
- [ ] Add music alpha P&L line to innait_status.py

### Day 15-21 (paper validation)
- [ ] Paper-trade 7 days across all open music markets
- [ ] Track: hit rate (target >55%), EV per trade (target >+3%)
- [ ] Kill any sub-genre where model worse than market
- [ ] Decision: proceed to live OR re-tune

### Day 22-30 (live small)
- [ ] $500 bankroll, $25 max per position
- [ ] Kill switch: 10% drawdown halts
- [ ] Day 28: scale to $2k if model holds in live conditions

## Niche LIP A/B test (parallel, 1 week)
- [ ] Modify top_n_to_quote: add `category_filter` param (music/weather/commodity only)
- [ ] Run 7 days in shadow mode (compute what we WOULD attack vs current)
- [ ] Compare snapshot share + projected rebate per dollar
- [ ] Decision: convert if niche-only beats shotgun by >30%

## Critical reminders
- Kalshi LIP sunsets Sept 1, 2026 (T-122 days)
- Music alpha must be live + validated by August
- Brandon Fean built $500 → $100k+ on this exact niche per Rolling Stone Jan 2026
- Caleb Davies $389k over 2 years
- Skip: feature markets (gossip), Trump mention markets (manipulated)

## Open items still pending from prior sessions
- [ ] PM bleed monitor enforcement (currently only logs)
- [ ] PM fill_ledger sync verification (deployed but 0 captured yet)
- [ ] MONEY_PRINT calibration recompute (currently inflated 23x)
- [ ] Y5 wire-in (PM scorer spread factor — function ready, callers need update)
- [ ] Hyperliquid HLP scout (Phase 2 if music alpha validates)
