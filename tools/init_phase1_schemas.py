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
        ("engine.calibration_ewma",       "market_calibration (per-market EWMA)"),
        ("cross_venue.hedger",            "hedge_log (cross-venue intentions/fills)"),
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

    # hedge_residual_log lives in the effectiveness tool — keep it adjacent
    try:
        from tools.hedge_effectiveness import ensure_schema as eff_ensure
        eff_ensure(db_path)
        print(f"  ✓ tools.hedge_effectiveness  (hedge_residual_log)")
    except Exception as e:
        print(f"  ✗ tools.hedge_effectiveness: {e}")
        return 1

    print("done. tables ready for paper run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
