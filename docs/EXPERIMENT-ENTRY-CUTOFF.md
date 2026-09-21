# Entry-cutoff paper experiment — results

Paper only. No order reached an exchange; live execution stayed blocked by
the interlock throughout, and `PRE_SETTLEMENT_CANCEL_MIN = 30` is unchanged
in `config/settings.py`. The variants exist only as a runner policy that
defaults to the control.

Pre-registration: policies `be3ad636dcbe6549` (`engine/entry_cutoff.py`),
selection spec `6ed709f8418a5d0e` (`engine/experiment_spec.py`). Both
fingerprints are written into every result file.

    control_30min       cutoff_min = 30.0            (unchanged live control)
    fixed_60s           cutoff_min = 1.0
    proportional_20pct  cutoff_min = min(30.0, max(1.0, 0.20 * duration_min))
                        duration_min = (close_time - open_time)/60, venue
                        unknown duration -> falls back to control

Run: `python tools/run_experiment.py --capture-sec 600`, or
`--stream <file>` to replay a captured stream.

## Correction to the claim that motivated this

I previously reported that "90 of the top 100 programs by reward rate are
blocked by the 30-minute pre-settlement gate". That computed the window
from the INCENTIVE period. It was the wrong clock and the conclusion did
not follow. Measured against the venue:

| market | incentive ends | market closes | ticker-parsed |
|---|---|---|---|
| `KXTEMPMIAH-26SEP2101-T72.99` | 55.9m | 55.9m | 55.9m |
| `KXTTELITEMATCH-…ASOMLU-ASO` | 85.9m | **10,105.9m** | −4.1m |
| `KXTRUMPPHOTO-26SEP27-7` | 1,435.9m | 10,675.9m | 9,834.9m |
| `KXCRYPTOLEAD15M-…0015-HYPE` | 10.9m | 10.9m | **None** |

A short reward window does not imply imminent settlement. Separately, the
gate was reading a close time regex-parsed from the ticker string, wrong by
days on live tickers, and `None` disabled the gate while the economics
assumed 24 hours to settle.

## Result 1 — the buffer was not the binding constraint

600-second capture, 12 markets (6 short-window, 6 long-dated), 1,308
snapshots, 1,200 trades, replayed through all three arms:

| arm | quotes | refusals |
|---|---|---|
| `control_30min` | 0 | pre_settlement 654, uneconomic 654 |
| `fixed_60s` | 0 | uneconomic 1,308 |
| `proportional_20pct` | 0 | uneconomic 1,308 |
| do nothing (baseline) | 0 | — |

The variants did exactly what they were designed to do: the
`pre_settlement` refusal disappears. **It changes which refusal fires, not
whether we quote.** Removing the buffer bought nothing, so the case for
relaxing the live risk control is not supported by this evidence.

A second capture (420s, 9 markets) produced no `pre_settlement` refusals in
any arm, so that sample does not discriminate between the policies at all.

## Result 2 — why the quotes are refused

Decomposition of the best candidate per long-dated market (assumptions are
carried with every figure in the result JSON):

| market | our_share | book depth | target | best net |
|---|---|---|---|---|
| `KXMLBSEASONGAMES-27-0` | 1.7e-05 | 59,562 | 1,000 | −$276.90 |
| `KXMLBSEASONGAMES-27-1215` | 2.2e-05 | 46,495 | 1,000 | −$0.06 |
| `KXMLBSEASONGAMES-27-1930` | 2.1e-05 | 47,025 | 1,000 | −$1,012.91 |
| `KXMLBSEASONGAMES-27-2425` | 1.6e-05 | 63,955 | 1,000 | −$822.73 |
| `KXMLBSEASONGAMES-27-600` | 2.2e-05 | 45,673 | 1,000 | −$187.60 |

The book is already ~50x deeper than the program's target, so
qualification is trivially satisfied and **our share of the pool is ~2e-05**
— about $0.00007 of expected reward over 18 hours. Any participation still
incurs adverse selection and fees. These markets are saturated: a $5,000
account cannot earn a material share of the pool.

This is a statement about these markets in these captures, not a verdict on
the strategy.

## What the modelled figures are and are not

**+$0.58 was a model estimate, and is superseded.** It was produced before
the execution model existed, under a placeholder of one full fill per
horizon, and reported without its assumptions. Every Economics record now
carries horizon, qualification terms, fee schedule and verified flag, fill
model and its caveat, and the inventory exit basis.

**Public trade volume is not our fill rate.** The execution model filters to
trades at our price level with the aggressor on the other side, requires
them to clear the depth ahead of us, subtracts latency, and reports three
queue cases (optimistic / base / conservative) because queue position is
unobservable. It bounds our executions from above; it does not establish
the probability that our order fills.

**No fill was manufactured.** Every modelled fill traces to a captured
public `trade_id`. Zero quotes means zero fills, and that is reported as
zero rather than dressed up.

## Known weaknesses in this evidence

1. **REST, not WebSocket.** No sequence, no queue position, no per-tick
   causality. This is the weakest part of the evidence and no credential is
   configured to fix it (see the runbook for the exact setup).
2. **Turnover realism.** The execution model returns fill counts in the
   thousands for a 1-contract quote at a heavily-traded level. Adverse
   selection and fees now scale together with turnover, so the model is
   internally consistent, but a quote refilled 12,000 times is not an
   operationally realistic policy and the absolute magnitudes should not be
   read as forecasts.
3. **No complete incentive period.** Captures are 7–10 minutes against
   programs lasting hours to months, so reward figures are partial and no
   subsequent inventory outcome is observed.
4. **Short-window coverage is thin.** The strongest capture had 6
   short-window markets; a later one had 3, and in the reused stream the
   short markets produced no economic evaluations at all.
5. **Fee rate unverified.** Rounding and maker-charging are verified;
   the rate constant is not, so fee-dependent figures are assumption-
   dependent and `fee_range_usd` reports a range.

## What would actually test the proposal

The cutoff cannot be evaluated on markets that are refused for economics
before the cutoff is ever reached. A real test needs markets where our
share of the pool is not ~2e-05 — that is, where book depth is comparable
to the program target rather than 50x it. The next measurement is therefore
a selection question, not a risk-tolerance question: find whether any live
program has a target within reach of a $5,000 account, and only then does
the entry cutoff become the binding constraint worth testing.

---

# Round 2 — census, corrected economics, and the real blocker

## Corrections to what I reported last round

**The saturation claim was wrong.** I said a book 50x the target dilutes our
share 50x. That is not the rule. Measured with `tools/audit_market_economics.py`
on `KXMLBSEASONGAMES-27-0`:

| | share |
|---|---|
| rules-based (lip_scorer) | 3.389e-05 |
| ratio form the economics used | 1.679e-05 |
| ratio / rules | **0.4953** |

The ratio form summed depth across BOTH sides into a one-sided denominator
and counted depth outside the cutoff that earns nobody anything. Reward now
comes from the program's rules.

**We never had to supply the target.** A ONE-contract order inside the
cutoff on an already-qualifying side earns a share. The "supply
`target - top`" candidate was removed; a shortfall candidate is offered only
when a side does not reach the target on its own.

**Two horizon/unit errors:** fill counts of 3,322–12,297 assumed instant
replenishment (now capped by re-quote cycle and the exit policy's net
limit), and adverse selection was priced over 437 days to settlement rather
than the ~1h the exit policy permits.

With all four corrected, the same MLB markets go from ≈−$1,000 to **positive**
modelled net. The earlier negatives were model artifacts.

## The actual blocker: a configuration mismatch

`risk/sentinel.py` sizes against `settings.BANKROLL_USD`. That is NOT an
absent constant — I said so first and it was wrong. It is

```python
BANKROLL_USD = float(os.getenv("LIP_BANKROLL", "80"))
```

an env-driven setting whose variable `LIP_BANKROLL` is **unset in this
environment**, so it falls back to its declared default of **80** while the
shared account ledger holds **$5,000**. settings.py even carries a comment
noting that several capital figures have coexisted.

The distinction matters: there is an intended mechanism for this
(`LIP_BANKROLL`), so the fix is a deliberate operator setting, not a code
change.

```
per-market cap = 10% x $80 = $8.00
sentinel size floor        = 10 contracts
=> feasible only if (yes + no) <= 80 cents
```

On a tight two-sided book (yes+no = 97–99c) the minimum legal size costs
$9.90 against an $8.00 cap. **The legal window is empty by $19 of bankroll**
— the largest legal size is 8 contracts against a floor of 10.

`LIP_BANKROLL` is NOT set here and `BANKROLL_USD` is NOT changed in code.
Reconciling the sentinel's bankroll with the $5,000 account is a risk
decision — raising it 62x to make quotes appear is precisely the loosening
I was told not to do. Whoever makes that call should note that every
sentinel cap (gross, per-market, per-series, daily loss) scales from this
one number.

## Opportunity census — the venue is NOT uniformly infeasible

`python tools/opportunity_census.py --book-sample 50`. Unique program ids,
active only, history never re-downloaded:

| stage | count |
|---|---|
| unique program ids (active) | 3,451 |
| program running now | 3,451 |
| unique markets | 3,451 |
| book-fetch budget (rate limit) | 50 |
| market open now | 50 |
| two-sided book | 41 |
| **sentinel-feasible** | **9** |

Rejections: one-sided book 9, sentinel-infeasible 32.

So **9 of 50 sampled markets admit a legal quote today** — the
`KXTRUMPMENTION-26SEP21-*` family, where yes+no is 65–80c and 10–12
contracts fit the $8 cap. My previous "0 of 9" came from a tight-spread
sample and should not have been generalised.

## Strongest candidates, and why I am not calling them an opportunity yet

| market | quote | capital | modelled net (24h) | share |
|---|---|---|---|---|
| `KXTRUMPMENTION-26SEP21-HOTT` | 10 @ 8/65 | $7.30 | **+$5.75** | 2.43e-02 |
| `KXTRUMPMENTION-26SEP21-SUPR` | 11 @ 7/61 | $7.48 | +$5.70 | 2.42e-02 |
| `KXTRUMPMENTION-26SEP21-CHIN` | 10 @ 21/57 | $7.80 | +$3.21 | 1.43e-02 |

The mechanism is real and follows from the rules: the top level holds only
~10 contracts while total depth still clears the 1,000 target, so our order
sits at zero distance from the reference and takes full weight. Distance
weighting rewards being at the top of a thin top-of-book, not supplying
size.

**Four reasons to distrust the magnitude.**

1. ≈128% return on capital per day is not a credible edge. A number that
   large usually means a model input is wrong, not that money is lying
   around.
2. The uncertainty allowance is already −$1.98 to −$3.38, i.e. a third of
   the modelled reward is being withheld for what we do not know.
3. Share is assumed to persist for 24 hours from one snapshot of a
   10-contract top level. If anyone joins, it collapses.
4. These are strikes of ONE event. Quoting several is one correlated bet,
   and the per-event cap applies.

## Break-even thresholds

`python tools/breakeven.py --stream <s> --experiment <e>`

* **Feasibility:** sentinel bankroll must reach **$99.00**; it is $80.00.
  Short by **$19.00**. At the ledger's $5,000 the cap would be $500,
  admitting ~505 contracts.
* **Economics:** the surviving candidates need only **0.21–0.23x** of the
  modelled share to break even — roughly a 4–5x margin — so the economic
  conclusion is not knife-edge on the share estimate.

## Is the obstacle measured economics, or missing information?

**Missing information, plus one configuration inconsistency.** Nothing here
shows the strategy losing money on its merits. It shows:

* a risk config whose bankroll disagrees with the funded account by 62x,
  leaving an empty legal window on tight books;
* an execution model that cannot see queue position, so the fill estimate
  carries a large declared uncertainty;
* an unverified fee rate.

None of those is a verdict about the edge. The next measurement is
prospective observation of the feasible candidates over complete incentive
windows.
