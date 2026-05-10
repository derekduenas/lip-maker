"""Generic backtest harness — replays settled markets through a brain.

Each brain backtest:
    1. Pull all settled Kalshi markets matching brain.market_pattern
    2. Reconstruct features at T-1 day before settlement (no lookahead)
    3. Score: Brier + reliability bins
    4. 80/20 chronological train/test split (or LOO if n < 100)
    5. Hyper-tune ONLY on train; report Test Brier as the gate metric

Phase 1 ships the runner skeleton; Phase 4 fills the data + scoring loops
once Music Brain has a model + features.
"""
