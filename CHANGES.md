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

---

# lipforge/oct10-nogo-fix (on d7426ca) — COMMAND 2026-10-10, Oct 10 gate = NO-GO

PAPER ONLY. One bundled change, then the build is FROZEN for 3 days (gate re-run ~Oct 14-15).
No live path, `resolve_mode`, `LIP_FORCE_PAPER`, kill switch or risk caps touched.

## Root causes (short bucket 0 selected, 51 `alloc_no_positive_step`)
Reproduced on a 3 h replay of the Oct 10 APEX recordings (d7426ca: 0.28 short / 0.17 durable
markets resting per selection).
1. `_size_curve` / `_quote` clamped a ONE-sided quote by the single-fill cap of the side it does
   not rest: an 8c YES quote next to an 88c NO book was capped at $25/0.88 = 28 contracts. With the
   30-90c band most quotes are one-sided, so every curve had one tiny rung, a tiny share, and fell
   under the payable floor. Now only resting sides bind (`_legal_size`).
2. `_payable_net` computed share = `share2 x len(sides)/2`, but `quote_economics` already returns our
   fraction of the snapshot credit ([0,1]) for one side and both: one-sided expected payouts were
   halved (e.g. $2.40 -> $1.20 < $1.50 floor -> reward valued 0).
3. The 0.5 uptime haircut cut the reward but not the fill-driven costs (adverse selection, fees,
   carry, rank-penalty increment), which only occur while resting. `LIP_PAYABLE_UPTIME_COSTS=1`
   scales both. Below the floor nothing changes (costs only, full rate).
After the fix the same replay rests 1.78 short / 6.33 durable markets per selection; 0 in-band sides.

## Other changes
| Flag (policy.conf) | Value | Code default | What |
|---|---|---|---|
| `LIP_SERIES_DENYLIST` | KXHURCAT,KXHURPATHAL,KXTRUMPENDORSEMENTS,KXNEXTTEAMNFL | empty | Excluded in `exclusion_reason` (`series_denylist`) and refused in `_quote`. Worst series by measured net. Held inventory still exits via paper exits. |
| `LIP_AVOID_BAND_LO/HI` | 30 / 90 (unchanged) | off | Band now also enforced on the PLACED price inside `_quote` (skew, AS back-off, join-touch, repeg, sampling): d7426ca still placed in-band sides on re-quotes (4 in the replay). Choice: STOP quoting 30-89c rather than widen, because widening keeps paying the measured -0.55 to -0.86c/contract markout for a reward that is mostly under the $1 floor. Bucket edges match `price_bucket` (70-90 = [70,90)). |
| `LIP_AVOID_BAND_REDUCE` | 0 | 1 | 0 = strict: reducing quotes are not rested inside the band either; aged / near-event inventory exits via the paper taker exits. |
| `LIP_FAVOR_LOW_CENTS` / `LIP_FAVOR_LOW_BOOST` | 10 / 1.0 | 0 / 0 (off) | Allocation order: a quote resting a side < 10c gets 2x priority in the greedy budget pass (never turns a non-positive value positive). The one-sided fill-cap fix also lets cheap sides carry their real size. |
| `LIP_PAYABLE_UPTIME_COSTS` | 1 | 0 | See root cause 3. |

/status gains `selection_policy` (denylist, band, reduce flag, band drops in `_quote`, favour, uptime-costs).

## KXBROSFT-26OCT08-T106 (unsettled ~40 h+ past close)
Exchange-side, not ours. Kalshi API (checked 2026-10-10 07:30 PT): status `closed`, `result` "",
`expected_expiration_time` = `latest_expiration_time` = 2026-10-15T05:30Z (Oct 14 22:30 PT); the source
(Carbon Arc September foot traffic) has not been reported. Close time 2026-10-08T03:59Z was the trading
close, not settlement. Our position: 36 NO @5c ($1.80 cost); last trade 99c YES. The engine keeps it
open and will book it when the settlement arrives; no code change. The "unsettled Nh past close"
alert is expected noise until Oct 15.
