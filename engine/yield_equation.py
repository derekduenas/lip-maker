"""Re-export shim — canonical source is cross_venue/yield_equation.py.
DO NOT add logic here. Edit the canonical copy instead.
"""
import sys, os
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)
from cross_venue.yield_equation import (  # noqa: F401
    MarketYield, KALSHI_CALIB, PM_CALIB, rank_markets, rank_by_yield_pct,
)
