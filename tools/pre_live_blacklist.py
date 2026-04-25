"""Seed the market_blacklist with audit-identified underperformers.

Reads the quality audit JSON and inserts auto_drop tickers into the
market_blacklist table with reason 'pre_live_audit'. The quote_manager
already consults this table before placing quotes.

Run once before going live:
    PYTHONPATH=. venv/bin/python tools/pre_live_blacklist.py --hours 24 --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from tools.market_quality_audit import analyze
from execution.market_blacklist import add_block, init_schema

_log = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--apply", action="store_true",
                   help="actually insert into market_blacklist")
    p.add_argument("--block-hours", type=int, default=48,
                   help="how many hours to block for (re-run audit after)")
    a = p.parse_args()

    report = analyze(hours=a.hours)
    auto_drop = report["auto_drop"]

    print(f"\n══════════════════════════════════════════════════════")
    print(f"  PRE-LIVE BLACKLIST SEEDING")
    print(f"══════════════════════════════════════════════════════")
    print(f"  Audit window:          {a.hours}h")
    print(f"  Auto-drop candidates:  {len(auto_drop)}")
    print(f"  Block duration:        {a.block_hours}h (re-audit after)")
    print(f"  Will apply?            {'YES' if a.apply else 'DRY RUN (use --apply)'}")
    print()

    init_schema(settings.DB_PATH)

    for entry in sorted(auto_drop, key=lambda x: -x["proj_daily"])[:10]:
        print(f"  {entry['ticker'][:40]:<42s}  {entry['reason']}")
    if len(auto_drop) > 10:
        print(f"  ... and {len(auto_drop) - 10} more")

    if not a.apply:
        print("\n  DRY RUN — not inserting. Add --apply to commit.")
        return

    inserted = 0
    for entry in auto_drop:
        reason = f"pre_live_audit:{entry['reason']}"
        add_block(entry["ticker"], reason, hours=a.block_hours, db_path=settings.DB_PATH)
        inserted += 1

    print(f"\n  ✅ {inserted} markets blacklisted for {a.block_hours}h")
    print(f"  Re-audit after {a.block_hours}h to reconsider them with more data.")


if __name__ == "__main__":
    main()
