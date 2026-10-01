"""Ranked Kalshi pool table. Does not call the network.

    python3 tools/pool_report.py --demo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mm.selector import demo_selection, render_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi pool ranking")
    parser.add_argument("--demo", action="store_true",
                        help="print a deterministic paper ranking")
    args = parser.parse_args(argv)
    if not args.demo:
        parser.print_help()
        return 2
    sys.stdout.write(render_report(demo_selection()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
