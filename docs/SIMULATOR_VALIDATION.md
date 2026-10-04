# Paper fill simulator: sensitivity band and how to validate it

Status 2026-10-04. Paper only. Nothing here sends an order. The simulator
(`execution/paper_fills.py`) has NEVER been compared with real fills, so every
expected-fill number it produces is a model output, not a measurement.

## Models (`LIP_SIM_QUEUE_MODEL`, default `depletion` = unchanged behaviour)

| model | rule | direction |
|---|---|---|
| `risk_averse` | only public prints advance the queue ahead of us; cancellations ahead are never assumed | most pessimistic |
| `depletion` (default) | prints advance the queue; the queue is also clipped to the size still displayed at our level | pessimistic |
| `prob_power` (`LIP_SIM_QUEUE_POWER` = n) | a decrease of the displayed level is attributed ahead of us with probability a^n / (a^n + b^n), a = queue ahead, b = size behind us | n=1 most optimistic; larger n credits less depletion |

`LIP_SIM_CANCEL_LATENCY_MS` (default 0): a cancelled or replaced paper order stays
fillable for that long (the stale-cancel pick-off). Order latency is the existing
`latency_ms` (250 ms).

The family follows hftbacktest's queue models (risk-averse / probability, f(x)=x^n,
typical n 1..3). Sources were search summaries: the pages could not be fetched,
so treat the exact definitions as ours, documented above and unit-tested in
`tests/test_sim_queue_models.py`.

## The band

```
python -m mm.replay bench --queue-band --since-hours 24 --json band.json
```

Replays the same recordings (`LIP_RECORD_ENABLE=1`) through the production
selection/quoting/accrual code once per model and prints `fills min..max` and
`net min..max`. Read it as a RANGE of plausible fill counts for the same
quotes. Only the queue model differs between rows.

## Using the band for the Oct 6 checkpoint ("30 real fills")

* The checkpoint counts REAL fills (live prints reaching our paper queue), not
  replay fills. The band tells you what to expect: if even the optimistic end
  predicts far fewer than 30 fills for the quoted set, the shortfall is the
  quote set (size/queue/volume), not bad luck; widen the sampling group or move
  to smaller-queue markets.
* If real fills land near the optimistic end, the pessimistic default is
  under-counting; if they land below the pessimistic end, something else is
  wrong (placement, pulls, skew guard).
* The band does NOT validate adverse selection: a paper fill's markout comes from
  the same recorded book either way.

## Validating against real fills later (procedure only)

Tiny live orders are NOT allowed now (`live_armed=false`, paper only), and nothing
here enables them. When a live test is eventually approved by the owner:

1. Log every order, cancel and fill with exchange timestamps; record the book and
   trades for the same period (`bookrec`).
2. Replay that period in the simulator with each queue model.
3. Compare, per market and queue position bucket: fill rate, time to fill,
   fraction filled by prints vs cancellations, and the 1 m / 5 m markout
   distribution of fills.
4. Choose the model (and n) whose fill rate and markouts match, with a confidence
   interval, and report the residual. Keep the pessimistic model for go/no-go
   decisions until the match is shown.

## Known gaps

No self-impact (our quotes never change competitors' behaviour or the displayed
book), no hidden/iceberg size, order latency is flat, and a print shared by several
of OUR orders at different prices is counted in full by each.
