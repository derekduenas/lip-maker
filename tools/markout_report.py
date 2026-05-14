"""Markout report — daily roll-up of fill_markouts by series.

Reads `fill_markouts` (populated by monitor.markout_logger) and emits
median / p25 / p75 / count of t+60s signed markout per series prefix
over a configurable lookback.

Phase 3 readiness gate reads this directly. Positive median markout =
the series is bleeding from adverse selection; negative = profitable
maker activity.

USAGE
-----
    python tools/markout_report.py --since 24h
    python tools/markout_report.py --since 7d --csv

EXIT CODES
----------
    0  → report emitted, any markout data found
    2  → report emitted but no fill_markouts rows in window
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings


def _parse_since(spec: str) -> float:
    """Parse '24h', '7d', '90m' → seconds. Default 24h."""
    m = re.match(r"^\s*(\d+)\s*([smhdw])\s*$", spec, re.IGNORECASE)
    if not m:
        raise ValueError(f"invalid --since: {spec!r}; use e.g. '24h', '7d'")
    n = int(m.group(1))
    unit = m.group(2).lower()
    mul = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return float(n * mul)


def _series_prefix(ticker: str) -> str:
    return ticker.split("-", 1)[0] if "-" in ticker else ticker


def summarize(db_path: str, lookback_sec: float) -> list[dict]:
    """Aggregate markout_60s by series_prefix.

    Returns list of dicts sorted by series with worst median markout last
    (so positive/toxic series surface clearly at the top).
    """
    import time as _time
    cutoff_ts = _time.time() - lookback_sec
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            """SELECT ticker, side, fill_size, markout_60s_c
               FROM fill_markouts
               WHERE fill_ts >= ? AND markout_60s_c IS NOT NULL""",
            (cutoff_ts,),
        ).fetchall()
    except sqlite3.OperationalError:
        # Table may not exist yet
        conn.close()
        return []
    conn.close()

    by_series: dict[str, list[float]] = {}
    size_by_series: dict[str, int] = {}
    for ticker, side, size, mo in rows:
        prefix = _series_prefix(ticker)
        by_series.setdefault(prefix, []).append(float(mo))
        size_by_series[prefix] = size_by_series.get(prefix, 0) + int(size or 0)

    out: list[dict] = []
    for prefix, vals in by_series.items():
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        median = statistics.median(vals_sorted)
        p25 = vals_sorted[n // 4] if n >= 4 else vals_sorted[0]
        p75 = vals_sorted[(3 * n) // 4] if n >= 4 else vals_sorted[-1]
        out.append({
            "series": prefix,
            "n_fills": n,
            "total_contracts": size_by_series[prefix],
            "median_markout_60s_c": round(median, 3),
            "p25_c": round(p25, 3),
            "p75_c": round(p75, 3),
            "toxic_pct": round(100.0 * sum(1 for v in vals if v > 0) / n, 1),
        })
    # Sort by median markout descending (worst on top)
    out.sort(key=lambda r: -r["median_markout_60s_c"])
    return out


def emit_table(rows: list[dict]) -> None:
    if not rows:
        print("(no fill_markouts in window — system may be paper-only or "
              "fill_markouts table empty)")
        return
    print(f"{'series':18s} {'n':>5s} {'sz':>6s} {'med_60s':>9s} "
          f"{'p25':>8s} {'p75':>8s} {'toxic%':>7s}")
    for r in rows:
        print(f"{r['series']:18s} {r['n_fills']:>5d} {r['total_contracts']:>6d} "
              f"{r['median_markout_60s_c']:>+9.2f} "
              f"{r['p25_c']:>+8.2f} {r['p75_c']:>+8.2f} "
              f"{r['toxic_pct']:>6.1f}%")


def emit_csv(rows: list[dict]) -> None:
    if not rows:
        return
    w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", default="24h",
                   help="lookback window, e.g. '24h' or '7d' (default: 24h)")
    p.add_argument("--db", default=settings.DB_PATH, help="sqlite path")
    p.add_argument("--csv", action="store_true", help="emit CSV to stdout")
    a = p.parse_args()
    lookback_sec = _parse_since(a.since)
    rows = summarize(a.db, lookback_sec)
    print(f"# markout report — last {a.since} "
          f"(generated {datetime.now(timezone.utc).isoformat()})", file=sys.stderr)
    print(f"# negative = benign maker fills; positive = informed flow picked us off",
          file=sys.stderr)
    if a.csv:
        emit_csv(rows)
    else:
        emit_table(rows)
    return 0 if rows else 2


if __name__ == "__main__":
    sys.exit(main())
