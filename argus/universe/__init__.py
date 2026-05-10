"""Market universe — crawler + brain router.

The crawler walks Kalshi's market list periodically; the router asks each
registered brain whether it claims a given ticker. The result is a queue
of (market, brain) pairs feeding the predict pipeline.

PHASE 1: stubs. Crawler implementation lands in Phase 5 (paper-mode scanner).
"""
