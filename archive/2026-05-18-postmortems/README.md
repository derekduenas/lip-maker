# Pre-innait branch archive postmortems — 2026-05-18

These two branches were archived per the innait founding plan (Section F).
The work was honored: code preserved as git tags (`archive/argus-2026-05-18`,
`archive/sovereign-2026-05-18`), rationale documented here.

When the `postmortem_log` table ships in Week 3-4 of the bring-up, these
entries get migrated to structured rows that the RAG retrieval system
surfaces to future agents.

---

## ARGUS — Music + Weather sentiment brains

**Branch**: `claude/argus-foundation`
**Scope**: 16 commits, ~4,900 LOC, 41 files (Music brain via Spotify Charts,
Weather brain via NWS, BSS-aware gate, paper-mode scanner, paper-position
writer + reconciler, validation dashboard, production supervisor)
**Tag**: `archive/argus-2026-05-18`

### Root cause for archival

Edge research (TradingAgents reference + edge-space scoring) found no
statistical alpha vs the crowd in sentiment-driven Kalshi markets:

- 71% of retail users **lose** money on sentiment / event-directional bets
- Top 1% capture 84% of gains (extreme power-law concentration)
- Music chart momentum → Kalshi sentiment-market correlation is too noisy
  at sub-hourly horizons to overcome bid-ask + Kalshi fees
- Sentiment signals decay sub-hourly; ARGUS's daily-batch cadence cannot
  capture the decay window
- No published evidence of profitable systematic sentiment trading at
  retail scale; institutional sentiment desks rely on alternative-data
  licensing innait cannot access

### Lesson (RAG-encoded)

> Lesson category: `strategy_selection`
>
> At $5-10K capital, do NOT pursue directional bets on sentiment-driven
> Kalshi markets. The market is too efficient at this tier; the crowd
> already prices the obvious signals. Edge requires either (a) alternative
> data licensing innait cannot afford, (b) sub-hourly execution speed
> innait cannot achieve, or (c) information asymmetry innait does not have.
>
> What still works at our scale: rebate-maker (LIP), cross-venue dislocation
> (Fed wedge), funding-rate carry. All three are mechanical / non-directional.
>
> Decay half-life: 360 days (timeless lesson about scale + edge access).

### What ARGUS did right (architectural value preserved)

- **BSS-aware gate**: Brier Skill Score before live capital — same
  validation discipline as Lens's walk-forward Sharpe gate
- **Brain abstraction** (`base.py` ABC + scrapers/ subdir): clean separation
  between data source and prediction logic. Reusable if a future
  alternative-data silo ever appears.
- **Paper-position writer + reconciler**: independent paper book separate
  from main LIP runner — pattern reused by Lens for walk-forward isolation
- **Production supervisor with systemd OnFailure**: pattern absorbed into
  innait's `dr/runbook.md` (Section L.3 of the plan)

### Recovery

```bash
git checkout archive/argus-2026-05-18 -- argus/ engine/argus/
```

---

## Sovereign — earnings / Fed / Trump mention market engine

**Branch**: `sovereign`
**Scope**: 12 commits, +20,452 / -34,034 LOC, 292 files. Earnings-call
mention market engine, NVDA/HIMS/WMT scheduler, continuous Fed+Trump
mention scanner, shadow_resolver persistence, rules-engine + Poisson
threshold model.
**Tag**: `archive/sovereign-2026-05-18`

### Root cause for archival (three reasons)

**1. No edge above the crowd.**
Same edge-research conclusion as ARGUS — earnings-mention markets are
directional bets where retail loses 71%+ of the time. The operator's own
Phase 4 commit was explicitly marked **"PARKED (corpus too thin)"** —
rolling-window math didn't generalize. The branch's audit fixes confirmed
the underlying signal-to-noise problem rather than solving it.

**2. Hostile fork of LIP execution layer.**
Branch deleted ~34K lines: `execution/kalshi_auth.py`, `execution/kalshi_ws.py`,
`quote_manager.py`, `monitor/alerts.py`, `monitor/reconciliation.py`,
`cross_venue/*` (7 files), `polymarket/execution/*`. Could not coexist
with the LIP infrastructure without 2-3 days of execution-layer refactoring.

**3. Misaligned with innait identity.**
The plan's identity section (Section I) commits innait to **mechanical /
non-directional / cross-venue / regulatory-niche** strategies. Sovereign is
the opposite: directional bets on event-cluster outcomes (earnings mentions,
Fed mentions, Trump mentions). It belonged to a different firm.

### Lesson (RAG-encoded)

> Lesson category: `branch_governance`
>
> Forks that delete the shared execution layer create maintenance debt
> that compounds. Either build new silos as ADDITIONS on top of the existing
> stack (the cross_venue / dislocation pattern), or fork into a wholly
> separate repository. Half-fork half-extension is the worst of both:
> can't run alongside, can't run independently.
>
> Decay half-life: 720 days (governance lesson; timeless at firm scale).

### What Sovereign did right (architectural value preserved)

- **Continuous scanner pattern** (drop fragile prefix list, trust
  `classify_market`): reusable shape for Recon's source-monitoring loop
- **Shadow_resolver persistence loop**: closes the trade→outcome→learn
  cycle — pattern reused by `postmortem_log` in Section N.2 of the plan
- **Rules-engine validation against live Kalshi markets**: pattern reused
  by `chaos/tier1_sentinel.py` red-team tests in Section O.1

### Recovery

```bash
git checkout archive/sovereign-2026-05-18
```

---

## What's NOT archived (and why)

- `claude/lip-fixes-saturday` — fully merged into the rebuild via `b430c0f`
  before the rebuild merged to main via `4d75a56`. No separate value to preserve.
- `claude/optimize-trading-system-MszGM` — strict subset of saturday-branch
  work; already merged through it. Pure duplicate. Marked for deletion
  (pending the operator running the delete on their own GitHub since the
  sandbox git server rejects destructive remote ops).

## Honor

The work on these branches is **not lost**. Both reflect real exploration
of legitimate edge hypotheses — they failed not because the engineering
was bad but because the underlying market structure denies their thesis
at our scale. innait keeps the patterns it can reuse and lets the rest
sleep in tags. If the market structure changes (alternative-data costs
drop, sentiment regimes become tradeable for small operators), these
branches are the starting point.
