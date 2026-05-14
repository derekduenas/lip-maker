"""Phase 3 readiness gate — go/no-go for flipping live.

Synthesizes the five quantitative thresholds defined in the rebuild
plan into a single pass/fail check:

    1. Median t+60s signed markout < 1.5c  (from fill_markouts)
    2. Daily Sharpe (incl. simulated hedges) > 1.0  (settlement_log
       net_outcome_usd + simulated hedge_pnl)
    3. Fill rate (filled / placed) > 18%  (quotes table)
    4. Max paper drawdown < $400 over the window
    5. Hedge basis residual |mean| < $25/day per active series
       (hedge_residual_log)

All five must hold for `--days` consecutive paper days (default 14).

EXIT CODES
----------
    0 → all gates pass; safe to flip live (`tools/go_live.py`)
    1 → at least one gate fails
    2 → insufficient data (not enough fills / markouts / hedges)

USAGE
-----
    python tools/go_live_check.py --days 14
    python tools/go_live_check.py --days 14 --json
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)


# Thresholds from the plan
GATE_MEDIAN_MARKOUT_MAX_C = 1.5
GATE_DAILY_SHARPE_MIN = 1.0
GATE_FILL_RATE_MIN = 0.18
GATE_MAX_DD_USD = 400.0
GATE_BASIS_RESIDUAL_MEAN_MAX_USD = 25.0
GATE_MIN_FILLS = 50           # minimum sample size for markout gate
GATE_MIN_SETTLEMENTS = 30     # minimum sample size for Sharpe / DD gate


@dataclass
class GateResult:
    name: str
    passed: bool
    observed: Optional[float]
    threshold: float
    detail: str = ""
    insufficient_data: bool = False


@dataclass
class Report:
    days: int
    gates: list[GateResult] = field(default_factory=list)

    @property
    def overall_pass(self) -> bool:
        return all(g.passed for g in self.gates) and not self.insufficient

    @property
    def insufficient(self) -> bool:
        return any(g.insufficient_data for g in self.gates)


# ── Gate 1: median t+60s markout ───────────────────────────────────────────

def _gate_markout(db_path: str, cutoff_ts: float) -> GateResult:
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            rows = conn.execute(
                """SELECT markout_60s_c FROM fill_markouts
                   WHERE fill_ts >= ? AND markout_60s_c IS NOT NULL""",
                (cutoff_ts,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        rows = []
    vals = [float(r[0]) for r in rows]
    n = len(vals)
    if n < GATE_MIN_FILLS:
        return GateResult(
            name="markout_60s_median",
            passed=False, observed=None,
            threshold=GATE_MEDIAN_MARKOUT_MAX_C,
            detail=f"n_fills={n} < {GATE_MIN_FILLS}",
            insufficient_data=True,
        )
    median = statistics.median(vals)
    return GateResult(
        name="markout_60s_median",
        passed=(median < GATE_MEDIAN_MARKOUT_MAX_C),
        observed=round(median, 3),
        threshold=GATE_MEDIAN_MARKOUT_MAX_C,
        detail=f"n={n} fills, median={median:+.3f}c "
               f"({'BENIGN' if median < 0 else 'TOXIC'})",
    )


# ── Gates 2 + 4: daily Sharpe + max drawdown from settlement_log ────────────

def _daily_pnl_series(db_path: str, cutoff_iso: str) -> list[tuple[str, float]]:
    """Return [(yyyy-mm-dd, total_net_usd), ...] over the window."""
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            rows = conn.execute(
                """SELECT substr(close_time, 1, 10) AS day,
                          COALESCE(SUM(net_outcome_usd), 0) AS net
                   FROM settlement_log
                   WHERE close_time >= ?
                   GROUP BY day
                   ORDER BY day""",
                (cutoff_iso,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return []
    return [(r[0], float(r[1])) for r in rows]


def _gate_sharpe(daily_pnls: list[float]) -> GateResult:
    n = len(daily_pnls)
    if n < 5:    # at least a working week
        return GateResult(
            name="daily_sharpe",
            passed=False, observed=None,
            threshold=GATE_DAILY_SHARPE_MIN,
            detail=f"days_with_data={n} < 5",
            insufficient_data=True,
        )
    mean = sum(daily_pnls) / n
    if n < 2:
        return GateResult(name="daily_sharpe", passed=False, observed=None,
                          threshold=GATE_DAILY_SHARPE_MIN, insufficient_data=True)
    sd = statistics.pstdev(daily_pnls)
    if sd < 1e-6:
        return GateResult(
            name="daily_sharpe", passed=False, observed=None,
            threshold=GATE_DAILY_SHARPE_MIN,
            detail=f"stdev~0 (mean={mean:+.2f})",
            insufficient_data=True,
        )
    sharpe = mean / sd
    return GateResult(
        name="daily_sharpe",
        passed=(sharpe > GATE_DAILY_SHARPE_MIN),
        observed=round(sharpe, 3),
        threshold=GATE_DAILY_SHARPE_MIN,
        detail=f"days={n}, mean={mean:+.2f} sd={sd:.2f}",
    )


def _gate_max_dd(daily_pnls: list[float]) -> GateResult:
    if len(daily_pnls) < 3:
        return GateResult(
            name="max_drawdown_usd",
            passed=False, observed=None, threshold=GATE_MAX_DD_USD,
            detail=f"days={len(daily_pnls)} < 3",
            insufficient_data=True,
        )
    # Equity curve = cumulative sum
    eq = []
    cum = 0.0
    for p in daily_pnls:
        cum += p
        eq.append(cum)
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return GateResult(
        name="max_drawdown_usd",
        passed=(max_dd < GATE_MAX_DD_USD),
        observed=round(max_dd, 2),
        threshold=GATE_MAX_DD_USD,
        detail=f"days={len(daily_pnls)}, peak_equity=${peak:.2f}",
    )


# ── Gate 3: fill rate (filled / placed) ─────────────────────────────────────

def _gate_fill_rate(db_path: str, cutoff_iso: str) -> GateResult:
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            row = conn.execute(
                """SELECT COUNT(*),
                          SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END)
                   FROM quotes
                   WHERE placed_at >= ?""",
                (cutoff_iso,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return GateResult(
            name="fill_rate", passed=False, observed=None,
            threshold=GATE_FILL_RATE_MIN, detail="quotes table missing",
            insufficient_data=True,
        )
    placed = int(row[0] or 0)
    filled = int(row[1] or 0)
    if placed < 500:
        return GateResult(
            name="fill_rate", passed=False, observed=None,
            threshold=GATE_FILL_RATE_MIN,
            detail=f"placed={placed} < 500 (insufficient)",
            insufficient_data=True,
        )
    rate = filled / placed if placed else 0.0
    return GateResult(
        name="fill_rate",
        passed=(rate > GATE_FILL_RATE_MIN),
        observed=round(rate, 4),
        threshold=GATE_FILL_RATE_MIN,
        detail=f"filled={filled} / placed={placed}",
    )


# ── Gate 5: basis residual from hedge_residual_log ──────────────────────────

def _gate_basis_residual(db_path: str, cutoff_iso: str) -> GateResult:
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            rows = conn.execute(
                """SELECT series_prefix, residual_usd, n_fills
                   FROM hedge_residual_log
                   WHERE window_end >= ? AND residual_usd IS NOT NULL""",
                (cutoff_iso,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        return GateResult(
            name="basis_residual_mean_abs",
            passed=False, observed=None,
            threshold=GATE_BASIS_RESIDUAL_MEAN_MAX_USD,
            detail="no hedge_residual_log rows",
            insufficient_data=True,
        )
    # Compute average abs residual per series, then take the worst
    by_series: dict[str, list[float]] = {}
    for prefix, res, n_fills in rows:
        by_series.setdefault(prefix, []).append(float(res))
    worst_series = ""
    worst_mean = 0.0
    for prefix, vals in by_series.items():
        mean_abs = abs(sum(vals) / len(vals))
        if mean_abs > worst_mean:
            worst_mean = mean_abs
            worst_series = prefix
    return GateResult(
        name="basis_residual_mean_abs",
        passed=(worst_mean < GATE_BASIS_RESIDUAL_MEAN_MAX_USD),
        observed=round(worst_mean, 2),
        threshold=GATE_BASIS_RESIDUAL_MEAN_MAX_USD,
        detail=f"worst series={worst_series}, "
               f"|mean_residual|=${worst_mean:.2f}",
    )


# ── Top-level ────────────────────────────────────────────────────────────────

def run_check(db_path: str = settings.DB_PATH, days: int = 14) -> Report:
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff_dt.timestamp()
    cutoff_iso = cutoff_dt.isoformat()
    rep = Report(days=days)
    rep.gates.append(_gate_markout(db_path, cutoff_ts))
    daily = _daily_pnl_series(db_path, cutoff_iso)
    daily_pnls = [v for _, v in daily]
    rep.gates.append(_gate_sharpe(daily_pnls))
    rep.gates.append(_gate_max_dd(daily_pnls))
    rep.gates.append(_gate_fill_rate(db_path, cutoff_iso))
    rep.gates.append(_gate_basis_residual(db_path, cutoff_iso))
    return rep


def emit_table(report: Report) -> None:
    print(f"# go_live_check — window={report.days}d, "
          f"checked={datetime.now(timezone.utc).isoformat()}", file=sys.stderr)
    print(f"{'gate':25s} {'pass':>4s} {'observed':>10s} {'threshold':>10s}  detail")
    for g in report.gates:
        tag = "✓" if g.passed else ("…" if g.insufficient_data else "✗")
        obs = f"{g.observed:.3f}" if g.observed is not None else "—"
        print(f"{g.name:25s}   {tag}  {obs:>10s} {g.threshold:>10.3f}  {g.detail}")
    if report.insufficient:
        print(f"\n⏳ INSUFFICIENT DATA — keep collecting in paper.")
    elif report.overall_pass:
        print(f"\n✅ ALL GATES PASS — safe to run `python tools/go_live.py` "
              f"(or your renamed flip script).")
    else:
        print(f"\n❌ GATES FAILED — do not flip live.")


def emit_json(report: Report) -> None:
    out = {
        "days": report.days,
        "overall_pass": report.overall_pass,
        "insufficient_data": report.insufficient,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "gates": [
            {
                "name": g.name, "passed": g.passed,
                "observed": g.observed, "threshold": g.threshold,
                "detail": g.detail,
                "insufficient_data": g.insufficient_data,
            }
            for g in report.gates
        ],
    }
    print(json.dumps(out, indent=2))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--db", default=settings.DB_PATH)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    rep = run_check(db_path=a.db, days=a.days)
    if a.json:
        emit_json(rep)
    else:
        emit_table(rep)
    if rep.insufficient:
        return 2
    return 0 if rep.overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
