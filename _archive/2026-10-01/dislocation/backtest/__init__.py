"""Backtest harness for the dislocation prong.

Two independent validation gates:

  1. DECOMPOSITION BACKTEST (decomposition.py)
     Validates the FedWatch math itself. For each historical FOMC:
       - Run the model at T-30, T-7, T-1 days using historical ZQ settles.
       - Compare predicted bucket probabilities to the realized outcome
         (one-hot vector — the bucket that actually happened gets 1.0).
       - Aggregate: mean absolute error per bucket, top-pick hit rate,
         calibration curve.
     This validates the PRICING model independently of Kalshi data.
     Lots of historical data available (~36 meetings 2022-2026).

  2. CONVERGENCE BACKTEST (convergence.py)
     Validates the trade premise: "Kalshi mispricing converges to ZQ
     by settlement." Uses the operator's existing settlement_log table
     for any Kalshi rate-decision markets they were active in.
     Limited data (Kalshi LIP started 2025), but each datapoint is
     directly relevant to live PnL.

Both feed into the LIVE_MODE gate: 30+ settlements within ±5pp band
required before auto-execution unlocks.
"""
