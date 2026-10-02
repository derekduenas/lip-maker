"""Ensure all Phase 1 / Phase 2 schemas exist before paper run.

Each new module's ensure_schema() is idempotent. Run this once after
pulling the branch on a deploy box; subsequent runs are no-ops.

USAGE
-----
    python tools/init_phase1_schemas.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def main() -> int:
    db_path = settings.DB_PATH
    print(f"initializing schemas in {db_path}")

    modules = [
        ("monitor.markout_logger",        "fill_markouts + book_microprice_history"),
        ("monitor.market_throttle",       "market_throttle (graduated A.3 ladder)"),
        ("monitor.unrealized_pnl",        "unrealized_pnl_snapshot (live MTM)"),
        ("engine.calibration_ewma",       "market_calibration (per-market EWMA)"),
        ("cross_venue.kalshi_pm_map",     "kalshi_pm_manual_map (operator overrides)"),
        ("cross_venue.arb_scanner",       "cross_venue_arb_log (arbitrage opportunities)"),
        ("execution.ibkr_adapter",        "broker_health + broker_dry_run_log"),
    ]
    for mod_name, label in modules:
        try:
            mod = __import__(mod_name, fromlist=["ensure_schema"])
            mod.ensure_schema(db_path)
            print(f"  ✓ {mod_name}  ({label})")
        except Exception as e:
            print(f"  ✗ {mod_name}: {e}")
            return 1

    # hedge_log / hedge_residual_log (cross_venue.hedger, tools/hedge_effectiveness)
    # were archived 2026-10-01 to _archive/2026-10-01/: their live hedge/unwind
    # path had no interlock. go_live_check's basis gate therefore stays
    # insufficient-data (fail closed).

    print("done. tables ready for paper run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
