"""Re-export shim — canonical source is cross_venue/yield_equation.py.
DO NOT add logic here. Edit the canonical copy instead.

Phase 0 2026-05-14: this file used to contain a duplicate copy of the
implementation; converted to a true shim to eliminate divergence risk.
"""
import sys, os
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)
from cross_venue.yield_equation import (  # noqa: F401
    MarketYield, KALSHI_CALIB, PM_CALIB, rank_markets, rank_by_yield_pct,
    kalshi_calib_for, pm_calib_for,
)
