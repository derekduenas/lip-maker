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
