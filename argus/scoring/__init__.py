"""Scoring — Brier + calibration metrics for brain validation.

Two artifacts per brain backtest:
    Brier score       — overall predictive accuracy
                        (0 = perfect, 0.25 = coin flip)
    Calibration curve — predicted_p vs realized_freq across bins

A brain graduates to live mode only when:
    Test Brier <= BRIER_LIVE_GATE  AND
    n_test    >= MIN_BACKTEST_N    AND
    no calibration bucket > 2σ off the diagonal
"""
