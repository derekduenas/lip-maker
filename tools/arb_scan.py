"""Cross-venue Kalshi ↔ PM US arbitrage scanner (CLI + cron).

Wraps cross_venue.arb_scanner.scan() with logging + result printing.
Designed to be run on a systemd timer (default 60s cadence).

EXIT CODES
----------
    0 → ran successfully
    1 → fatal error
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from cross_venue.arb_scanner import scan


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=settings.DB_PATH)
    p.add_argument("--min-edge-c", type=int, default=1,
                   help="minimum edge in cents to log (default 1)")
    p.add_argument("--max-pairs", type=int, default=50,
                   help="cap on Kalshi tickers examined per scan (default 50)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()
    logging.basicConfig(
        level=logging.WARNING if a.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        summary = scan(
            db_path=a.db, min_edge_c=a.min_edge_c, max_pairs=a.max_pairs,
        )
    except Exception as e:
        logging.error(f"arb_scan failed: {e}")
        return 1
    if a.json:
        print(json.dumps(summary, indent=2))
    elif not a.quiet:
        print(f"# arb_scan: examined {summary['n_pairs_examined']} pairs, "
              f"found {summary['n_findings']} crossed spreads "
              f"(min_edge={a.min_edge_c}c)")
        for f in summary["top_findings"]:
            print(f"  {f['kalshi']:36s} ↔ {f['pm_slug']:40s} "
                  f"{f['arb_side']:>22s}  +{f['edge_c']}c  depth={f['depth']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
