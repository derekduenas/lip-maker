"""Execution layer — sizer + (future) order placement.

Phase 1 ships the sizer. Order placement is intentionally deferred until
a brain proves itself in paper mode (Test Brier <= 0.20, n >= 20).
"""
