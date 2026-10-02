# lip-maker

Paper-only market maker that quotes for liquidity-incentive rewards on
**Kalshi** (LIP) and **Polymarket US**. Nothing in this repository is
configured to trade live; see "Live" below.

## What runs (APEX)

Two processes on the APEX droplet, user `lip`, code in `/opt/lip-maker`,
state in `/var/lib/lip-maker`:

| process | unit | entrypoint |
|---|---|---|
| quote loop (paper) | `deploy/lip-unattended.service` + drop-ins in `deploy/apex/lip-unattended.service.d/` | `python -m mm.unattended --run ...` |
| watchdog / kill switch | `deploy/apex/lip-watchdog.service` | `python -m mm.safety.lip_watchdog` |

Policy knobs are the `LIP_*` variables in
`deploy/apex/lip-unattended.service.d/policy.conf` and
`deploy/apex/watchdog.env.example`. Offline replay of recorded frames:
`python -m mm.replay bench`. Install: `deploy/droplet/setup.sh`
(see `docs/DROPLET.md`).

## Layout: live code vs legacy

```
mm/                 APEX engine: unattended loop, selector, risk (mm/risk.py),
                    venues (Kalshi read-only, PM US paper), safety/watchdog, replay
engine/             shared math; APEX uses lip_scorer, lip_accrual, fees,
                    series_fees, lip_reconcile, lip_calibration, reward_provenance,
                    calibration_ewma (and lip_discovery._parse_program)
execution/          kalshi_auth, kalshi_ws, paper_fills, order_request (APEX);
                    quote_manager.py is LEGACY and paper-only (live raises)
polymarket/engine/  pm_us_lip_scorer (APEX)
config/             settings, constitution
---- legacy (not on the APEX path) ----
run_paper.py        old PaperRunner; executing it just runs mm.unattended
polymarket/         old PM runner (run_pm.py, paper-only: PM_PAPER=false is refused)
cross_venue/        old cross-venue tools; yield_equation.py is legacy
risk/sentinel.py    guards only the legacy QuoteManager
tools/, monitor/    mostly operator scripts for the old /root install
                    (exceptions imported by APEX: monitor/alerts.py,
                    tools/attack_targets.py, tools/competitor_density.py)
_archive/           moved-out code (see _archive/2026-10-01/README.md)
```

## Facts worth knowing

- **Paper only.** `mm.unattended` refuses production order hosts; the legacy
  `QuoteManager` and `run_pm.py` refuse live outright. Unit-file
  `Environment=LIP_PAPER=true` does not override the env file
  (systemd.exec(5)); see `docs/DROPLET.md`.
- **Kalshi LIP runs to Jan 1, 2027** (extended from Sept 1, 2026):
  https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program
- **Polymarket US incentives:** `GET /v1/incentives` exists. The engine
  reads it from `gateway.polymarket.us`, which is undocumented and
  unauthenticated; the documented API is `api.polymarket.us` with API
  keys. Treat the gateway as best-effort.
- **Calibration constants 0.25 (Kalshi) / 0.10 (PM)** in
  `cross_venue/yield_equation.py` are unmeasured priors, not fitted to paid
  rewards.
- **`cross_venue/yield_equation.py` is legacy.** It is a heuristic, not either
  venue's scoring formula, and it is not the live ranking (APEX scores with
  `engine/lip_scorer.py` and `polymarket/engine/pm_us_lip_scorer.py`).
- Reward figures are estimates until reconciled against a venue payment
  (`engine/reward_provenance.py`).

## Live

There is no supported live path in this repository today. The former
"$20k/mo by Sept 1, 2026" goal line is past and is not a current target.

## Tests

```bash
python3 -m pytest -q -p no:cacheprovider
```

Runtime deps: `requirements.txt`; test deps: `requirements-dev.txt`.
