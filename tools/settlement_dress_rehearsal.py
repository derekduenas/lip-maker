"""Settlement dress rehearsal — preview Tue Apr 28 weekly payout via Mon settle data.

Mon Apr 27 22:00 UTC = first reconciler run after weekend → Apr 27 commodity
settlements land. That single day's realized PnL + series mix is the
strongest signal we'll get pre-Tuesday weekly payout.

Run anytime after 22:00 UTC Monday:
  python tools/settlement_dress_rehearsal.py             # today UTC
  python tools/settlement_dress_rehearsal.py --day 2026-04-27

Output: per-day realized vs baseline ($246/day deep-dive expected),
series breakdown, and interpretation for Tue payout band.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


# Baselines from frozen Apr 28 prediction memory file
EXPECTED_DAILY_PNL_USD = 246.0   # deep-dive corrected daily rate
WEEKLY_BAND_LOW = 1700.0
WEEKLY_BAND_HIGH = 2400.0
WEEKLY_POINT = 2000.0


@dataclass
class DayReport:
    day: str
    n_settled: int
    realized_total: float
    rebate_total: float
    n_winners: int
    n_losers: int
    by_series: list[tuple[str, int, float]]  # (series, n, realized)


def fetch_day(day: str, db_path: str = settings.DB_PATH) -> DayReport:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            """SELECT series_prefix, our_realized_usd, COALESCE(rebate_earned_usd, 0)
               FROM settlement_log
               WHERE date(close_time) = ?
                 AND (our_position_yes > 0 OR our_position_no > 0)""",
            (day,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return DayReport(day, 0, 0.0, 0.0, 0, 0, [])

    realized = sum(r[1] or 0 for r in rows)
    rebate = sum(r[2] or 0 for r in rows)
    winners = sum(1 for r in rows if r[1] > 0)
    losers  = sum(1 for r in rows if r[1] < 0)

    by_series: dict[str, list[float]] = {}
    for series, pnl, _ in rows:
        by_series.setdefault(series, []).append(pnl or 0)
    series_summary = sorted(
        [(s, len(v), round(sum(v), 2)) for s, v in by_series.items()],
        key=lambda x: x[2],
    )

    return DayReport(
        day=day, n_settled=len(rows),
        realized_total=round(realized, 2),
        rebate_total=round(rebate, 2),
        n_winners=winners, n_losers=losers,
        by_series=series_summary,
    )


def interpret_for_weekly(day_realized: float) -> str:
    """Translate single-day realized PnL into Tue weekly payout band guess."""
    # A "bad day" (-$50 to -$90, like Apr 24) suggests low end
    # A "flat day" (~$0) suggests mid range
    # A "good day" (+$20+) suggests high range
    if day_realized < -50:
        return ("LOW BAND likely. Single-day loss similar to Apr 24 (-$90.61). "
                f"Tue weekly payout estimate: ${WEEKLY_BAND_LOW:.0f} or below.")
    if day_realized < -10:
        return ("LOW-MID range. Modest single-day loss. "
                f"Tue weekly payout estimate: ${WEEKLY_BAND_LOW:.0f}-${WEEKLY_POINT:.0f}.")
    if day_realized < 10:
        return ("MID range. Roughly flat day. "
                f"Tue weekly payout estimate: ${WEEKLY_POINT:.0f} ± $200 (deep-dive central).")
    return ("HIGH BAND likely. Profitable settled day. "
            f"Tue weekly payout estimate: ${WEEKLY_POINT:.0f}-${WEEKLY_BAND_HIGH:.0f} or higher.")


def print_report(report: DayReport) -> None:
    print()
    print("══════════════════════════════════════════════════════════════")
    print(f"  SETTLEMENT DRESS REHEARSAL — {report.day}")
    print("══════════════════════════════════════════════════════════════")
    if report.n_settled == 0:
        print(f"  ⚠️  No settled positions for {report.day}.")
        print(f"     Either: (a) no markets closed that day, OR")
        print(f"             (b) reconciler hasn't picked them up yet.")
        print()
        return

    print(f"  Settled positions:    {report.n_settled}")
    print(f"  Winners / Losers:     {report.n_winners} W / {report.n_losers} L")
    print(f"  REALIZED TOTAL:       ${report.realized_total:+,.2f}")
    print(f"  Rebate accrual:       ${report.rebate_total:+,.2f}")
    print(f"  Net day:              ${report.realized_total + report.rebate_total:+,.2f}")
    print()
    print(f"  vs expected daily:    ${EXPECTED_DAILY_PNL_USD:+.2f} (deep-dive baseline)")
    delta = report.realized_total + report.rebate_total - EXPECTED_DAILY_PNL_USD
    print(f"  variance vs baseline: ${delta:+.2f}")
    print()
    print("  ── By series (worst first) ──")
    for series, n, pnl in report.by_series:
        flag = "🔴" if pnl < 0 else "🟢"
        print(f"    {flag} {series:<14s} {n:>2d} markets  ${pnl:+8.2f}")
    print()
    print("  ── INTERPRETATION FOR TUE APR 28 WEEKLY PAYOUT ──")
    print(f"  {interpret_for_weekly(report.realized_total)}")
    print()
    print("══════════════════════════════════════════════════════════════")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--day", default=None,
                   help="UTC day YYYY-MM-DD (default: today)")
    a = p.parse_args()
    day = a.day or datetime.now(timezone.utc).date().isoformat()
    report = fetch_day(day)
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
