"""Risk controls — per-brain exposure caps + global circuit breaker.

Each brain is allotted a slice of the bankroll. Per-brain exposure can
grow within that slice. Cross-brain correlation hooks (limits.py)
prevent stacking on highly correlated markets.

PHASE 1 STUB. Full circuit breaker logic ships before any brain trades live.
"""
