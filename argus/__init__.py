"""ARGUS — Multi-domain directional prediction engine for Kalshi.

Hundred-eyed watcher: 11 domain brains (target), each predicting one Kalshi
market family better than retail flow. Pure directional alpha on free data.

Architecture:
    universe/   continuous market crawler + brain router
    brains/     DomainBrain ABC; one subclass per domain
    data/       venue-agnostic data adapters (Spotify, NWS, FDA, ...)
    scoring/    Brier + calibration metrics for brain validation
    backtest/   generic harness — any brain, any history
    execution/  Kelly sizer with confidence shrink + correlation guards
    risk/       per-brain exposure caps + global circuit breaker

Brains graduate to live trading only after Test Brier <= 0.20 on a
representative held-out sample (n >= 20).

Does NOT depend on / cannot interfere with:
    Prong 1 (LIP rebate harvester)
    Prong 2 (Sovereign earnings predictor)
    Prong 3 (Cross-venue dislocation)

CLI entry points:
    python -m tools.argus_backtest --brain music
    python -m tools.argus_scan
"""
