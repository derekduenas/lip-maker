# CHANGES — grok/lip-fixes-20261009

Branch `grok/lip-fixes-20261009` on top of `7cdfaf8`. Derek approved this on 2026-10-09: fix and redeploy, **PAPER ONLY**.

- No live order path was added or enabled.
- `resolve_mode` and `LIP_FORCE_PAPER` are unchanged.
- The new "exit" is a simulated paper booking inside `RunLoop._paper_exit`. It refuses to run unless `mode == "paper"`, and it never touches a transport.

## Preregistration (written before any replay or live evaluation)

The flags and defaults below were fixed **before** the offline replay or the redeploy was evaluated. The evaluation reports these numbers as they come out, and does not tune the flags against them:

- net **payable** rewards;
- 5-minute markout (adverse selection);
- P&L marked to the executable bid.

Any later change to these values needs a new entry with a reason.

| Fix | Flag (policy.conf) | Value | Code default | Rationale |
|---|---|---|---|---|
| a | (always on) | – | – | The headline `pnl_usd` and the go/no-go use **payable** rewards: each market-period is floored to the cent, and anything under $1 counts as $0. The old gross figure stays alongside as `pnl_gross_usd` and `est_rewards_gross_usd`. |
| b | `LIP_SAMPLE_ENABLE` | 0 | – | The probe has 1,358 fills and was starving the reward book. |
| b | `LIP_SAMPLE_MAX_FRAC` | 0.2 | 0.2 | If the probe is re-enabled, it can take at most 20% of the Kalshi budget. |
| b | `LIP_STARVED_BELOW_FRAC` | 0.25 | 0.25 | The starvation alarm fires when the reward budget is under 25% of the Kalshi cap (floor `LIP_STARVED_BELOW_USD`=$10). |
| c | `LIP_MARK_BASIS` | executable | executable | Marks the unpaired leg at what it would sell for now: walk the held side's bids, take the Kalshi taker fee off, and value pairs at $1. `markout_mid_usd` is kept. |
| c | `LIP_MARK_POLL_S` | 300 | 300 | A read-only REST order book for held markets that have no live book. |
| d | `LIP_INV_MAX_AGE_H` | 24 | 0 | After 24 h, unpaired inventory goes reduce-only with maximum skew toward exit. |
| d | `LIP_EXITS_PAPER_TAKER` | 1 | 0 | Paper taker exit of unpaired inventory. |
| d | `LIP_FLATTEN_AGE_H` | 72 | 0 | Exits inventory that is still unpaired after 72 h. |
| d | `LIP_FLATTEN_BEFORE_EVENT_H` | 6 | 0 | Exits, and stops adding, unpaired inventory within 6 h of the event day, occurrence, or close (same horizon as `LIP_EVENT_WINDOW_HOURS`). |
| d | `LIP_EXIT_MAX_SLIPPAGE_CENTS` | 5 | 5 | Exits only into bids within 5¢ of the best bid. |
| d | `LIP_EXIT_MAX_BOOK_AGE_S` | 300 | 300 | Exits only off a book that is at most 5 min old. |
| d | `LIP_AVOID_BAND_LO` / `HI` | 30 / 90 | off | No new inventory on a side priced 30–89¢. The measured 5-minute markout there is −0.86¢ (30–70) and −0.55¢ (70–90) per contract. |
| d | `LIP_INV_SOFT_CAP_USD` | (unset) | 0.8 × `LIP_WD_MAX_INVENTORY_USD` = $400 | Global reduce-only below the watchdog's hard limit. The limit itself is **not** raised. |
| d | `LIP_SKEW_ENABLE` | 1 | – | Already on (inventory-skewed quotes). |
| e | `LIP_EVENT_CALENDAR_FILE` | /etc/lip-maker/event_calendar.json | unset | Verified CPI, PPI, NFP and FOMC dates for Oct–Dec 2026. |
| e | `LIP_AS_GUARD_ENABLE` | 1 | 0 | Existing markout-EWMA guard on our own fills. |
| e | `LIP_TAPE_BURST_ENABLE` | 1 | 0 | New. Pulls the quote for 300 s if one side of the public tape takes at least 250 contracts in 60 s and that is at least 80% of the window's taker volume. |
| f | (always on) | – | – | The scorer counts only the size that reaches Target at the cutoff level, pro rata. |
| g | `LIP_PAYABLE_SELECT` | 1 | 0 | The plan values a market's reward at 0 unless the expected period payout reaches $1.50. Expected payout = accrued + share × pool × time left × 0.5. |
| g | `LIP_PAYABLE_UPTIME` | 0.5 | 0.5 | Haircut for measured uptime. |
| g | `LIP_MIN_PAYABLE_PER_PERIOD_USD` | 1.5 | 1.5 | Kalshi's $1 minimum plus a margin. |
| h | `LIP_PMUS_PAPER_ENABLE` | 0 | – | PM US off: 0 quotes all session, and the eligible pools are sports. |
| i | watchdog | – | – | Changes: inventory trip = **unpaired** cost (locked pair loss reported, not counted); early warning at 80%; engine alerts forwarded. |

### Decision rule fixed in advance

Over the paper period after the redeploy, the series and go/no-go report (`/status series_gate`) uses payable rewards and executable marks. A series is "go" only if all of these hold:

- the existing `series_go` criteria (days, fills, markout);
- net, computed with **payable** rewards, is > 0;
- the event-level 90% lower bound of the 5-minute markout plus the payable reward per measured contract is > 0.

Nothing arms anything.

## What changed (file:line on this branch)

See the final report for exact line numbers. In summary:

- `engine/lip_scorer.py` `_score_bids`: target cap at the cutoff level (fix f).
- `mm/unattended/loop.py`:
  - payable twins: `closed_periods_payable` and its agg, plus backfill from `period_estimates`;
  - `pnl_report` and `series_gate_report` use payable;
  - executable marks: `_exec_levels`, `_exec_value`, `note_quote_mark`, `mark_candidates`, `_mark_backfill`, `parse_kalshi_orderbook`;
  - the mark-basis switch rebases today's MTM;
  - inventory: `_side_blocked` gains soft cap, near-resolution and price-band checks; `_inventory_exits` and `_paper_exit` added;
  - `_tape_burst`;
  - `_payable_net` in `_size_curve`;
  - relative starvation, and the probe reserve capped at a fraction.
- `mm/unattended/service.py`: wires `mark_candidates` into the read-only driver.
- `mm/safety/lip_watchdog.py`: unpaired-only inventory, 80% warning, `forward_engine_alerts`, alert channel listing in health.
- `mm/status_page.py`: new status keys.
- `deploy/apex/lip-unattended.service.d/policy.conf`: the flags above.
- `deploy/apex/event_calendar.apex.json`: the calendar.

## Known limits

- **Payable floor for split periods.** The payable floor is applied per archived accrual window. A market-period split by a program re-feed or restart is floored per piece, so it is understated. This is conservative.
- **Exit depth.** Executable marks and exits only use the visible bid depth. Depth beyond the book is not assumed: contracts the book can't absorb are left unfilled and reported as `exec_depth_short_contracts`.
- **Uptime haircut.** `LIP_PAYABLE_UPTIME`=0.5 is a prior, not a fit. Revisit it with reward reconciliation once Kalshi pays.
- **Calendar maintenance.** The calendar needs 2027 dates before 2026-12-10.
