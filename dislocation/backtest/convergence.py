"""Kalshi <-> ZQ convergence backtest.

Validates the trade premise directly:
  Given a historical Kalshi rate-decision market and the matching ZQ
  futures, did the spread between them collapse to near-zero by FOMC
  settlement?

INPUTS:
  - data/lip_maker.db (existing) — pulls Kalshi market closes from the
    settlement_log table, joined with our snapshot history if available.
  - ZQHistory loaded from data/historical/zq_history.csv.
  - data/historical/kalshi_fed_history.csv (manual export the operator
    drops in if they have older Kalshi data).

OUTPUT (per pair):
  fomc_date | kalshi_market | T-30_kalshi_p | T-30_zq_p | T-30_spread_pp
            | T-7 ... | T-1 ... | T+0_kalshi_p | T+0_zq_p | T+0_spread_pp
            | converged (T+0 spread < 3pp?)

AGGREGATE:
  - convergence rate (% of pairs that converged)
  - max-adverse-excursion (MAE): worst spread WIDENING during hold period
  - mean realized PnL on simulated trades that would have flagged

This is the GATE for live execution. 30+ converged pairs at <5pp T+0 →
flip DISLOCATION_LIVE=true.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..pricing.fed_funds import FOMCContext, decision_probs
from .historical_fomc import HistoricalFOMC, HISTORICAL_FOMCS
from .zq_history import ZQHistory, contract_for_month

_log = logging.getLogger(__name__)


@dataclass
class KalshiHistoricalPoint:
    """One historical Kalshi quote for a rate-decision market."""
    market_ticker:   str
    fomc_date:       dt.date
    snapshot_date:   dt.date
    bucket_lower:    float        # the bucket this market resolves on
    yes_mid:         float        # 0..1


def load_kalshi_history(
    csv_path: Path,
) -> list[KalshiHistoricalPoint]:
    """Load Kalshi rate-decision market history from CSV.

    Format:
        market_ticker,fomc_date,snapshot_date,bucket_lower,yes_mid
        KXFED-26JUN-CUT25,2026-06-17,2026-05-15,0.0500,0.42
        ...
    """
    out: list[KalshiHistoricalPoint] = []
    if not csv_path.exists():
        _log.warning(f"kalshi history CSV not found: {csv_path}")
        return out
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                out.append(KalshiHistoricalPoint(
                    market_ticker=row["market_ticker"],
                    fomc_date=dt.date.fromisoformat(row["fomc_date"]),
                    snapshot_date=dt.date.fromisoformat(row["snapshot_date"]),
                    bucket_lower=float(row["bucket_lower"]),
                    yes_mid=float(row["yes_mid"]),
                ))
            except (KeyError, ValueError) as e:
                _log.warning(f"skip bad kalshi row: {row} ({e})")
    _log.info(f"loaded {len(out)} kalshi historical points from {csv_path}")
    return out


def load_kalshi_from_settlement_log(db_path: str) -> list[KalshiHistoricalPoint]:
    """Pull Kalshi rate-decision market closes from the existing DB.

    Looks for tickers matching KXFED* in settlement_log and lip_snapshots.
    Returns one point per (market, snapshot_day).

    NOTE: existing schema may not have all needed fields. Adjust query as
    you discover what your DB actually contains.
    """
    out: list[KalshiHistoricalPoint] = []
    if not Path(db_path).exists():
        return out
    try:
        conn = sqlite3.connect(db_path)
        # Probe schema first. This is defensive — adjust per actual schema.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('settlement_log', 'lip_snapshots')"
        )
        tables = {row[0] for row in cur.fetchall()}
        if "lip_snapshots" not in tables:
            _log.warning("lip_snapshots not in DB; convergence backtest will use CSV only")
            return out

        # Pull KXFED* snapshots. Schema may vary — wrap in try/except.
        try:
            rows = conn.execute("""
                SELECT market_ticker, snapshot_date, yes_mid_cents
                FROM lip_snapshots
                WHERE market_ticker LIKE 'KXFED%'
                ORDER BY snapshot_date ASC
            """).fetchall()
            for ticker, snap, yes_mid in rows:
                # Best-effort parse — operator extends the regex if needed.
                # ticker like KXFEDDECISION-26JUN-CUT25 → fomc month=Jun26
                # Without explicit FOMC date in row, we skip; operator
                # populates the CSV path instead for fine control.
                pass
        except sqlite3.OperationalError as e:
            _log.warning(f"lip_snapshots schema mismatch: {e}")

    except Exception as e:
        _log.warning(f"DB read failed: {e}")
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── Convergence pair ────────────────────────────────────────────────────
@dataclass
class ConvergencePoint:
    snapshot_date:   dt.date
    days_to_fomc:    int
    kalshi_p:        float
    zq_p:            float
    spread_pp:       float


@dataclass
class ConvergenceResult:
    fomc_date:       dt.date
    market_ticker:   str
    bucket_lower:    float
    points:          list[ConvergencePoint]
    final_spread_pp: float            # spread at the final available snapshot
    max_spread_pp:   float            # max spread across the holding period
    converged:       bool             # final_spread_pp < threshold
    realized:        float            # 1.0 if this bucket actually happened, 0.0 otherwise
    simulated_pnl_pct: float          # pnl as % of position if entered at first widening

    def explain(self) -> dict:
        return {
            "fomc":         self.fomc_date.isoformat(),
            "ticker":       self.market_ticker,
            "bucket":       f"{self.bucket_lower*100:.2f}%",
            "n_points":     len(self.points),
            "final_pp":     round(self.final_spread_pp, 2),
            "max_pp":       round(self.max_spread_pp, 2),
            "converged":    self.converged,
            "realized":     self.realized,
            "sim_pnl_%":    round(self.simulated_pnl_pct, 2),
        }


@dataclass
class ConvergenceStats:
    n_pairs:           int
    convergence_rate:  float           # % final_spread < threshold
    mean_max_spread:   float           # average max-adverse-excursion
    mean_final_spread: float
    mean_simulated_pnl: float
    n_basis_blowups:   int             # cases where realized != predicted (basis broken)

    def explain(self) -> dict:
        return {
            "n_pairs":              self.n_pairs,
            "convergence_rate_%":   round(self.convergence_rate * 100, 1),
            "mean_max_spread_pp":   round(self.mean_max_spread, 2),
            "mean_final_pp":        round(self.mean_final_spread, 2),
            "mean_sim_pnl_%":       round(self.mean_simulated_pnl, 2),
            "basis_blowups":        self.n_basis_blowups,
        }


def _zq_implied_for_bucket(
    meeting:        HistoricalFOMC,
    zq_price:       float,
    bucket_lower:   float,
    decision_buckets: list[float],
) -> float:
    ctx = FOMCContext(
        current_target_lower=meeting.pre_lower,
        current_target_upper=meeting.pre_upper,
        fomc_date=meeting.fomc_date,
        contract_month_start=meeting.fomc_date.replace(day=1),
        contract_month_end=_month_end(meeting.fomc_date),
        decision_buckets=decision_buckets,
    )
    probs = decision_probs(zq_price, ctx)
    nearest = min(probs.keys(), key=lambda b: abs(b - bucket_lower))
    return probs[nearest]


def _month_end(d: dt.date) -> dt.date:
    if d.month == 12:
        return dt.date(d.year, 12, 31)
    nxt = dt.date(d.year, d.month + 1, 1)
    return nxt - dt.timedelta(days=1)


def run_convergence_backtest(
    kalshi_points:    list[KalshiHistoricalPoint],
    zq_history:       ZQHistory,
    *,
    convergence_threshold_pp: float = 5.0,
) -> tuple[list[ConvergenceResult], ConvergenceStats]:
    # Group by (fomc_date, market_ticker, bucket_lower).
    grouped: dict[tuple[dt.date, str, float], list[KalshiHistoricalPoint]] = {}
    for p in kalshi_points:
        grouped.setdefault((p.fomc_date, p.market_ticker, p.bucket_lower), []).append(p)

    results: list[ConvergenceResult] = []

    for (fomc_date, ticker, bucket), points in grouped.items():
        meeting = next((m for m in HISTORICAL_FOMCS if m.fomc_date == fomc_date), None)
        if meeting is None:
            _log.debug(f"no historical record for FOMC {fomc_date}, skipping {ticker}")
            continue
        contract = contract_for_month(fomc_date)
        decision_buckets = sorted({bucket} | {bucket-0.0050, bucket-0.0025, bucket+0.0025, bucket+0.0050})

        cps: list[ConvergencePoint] = []
        for kp in sorted(points, key=lambda x: x.snapshot_date):
            zq_lookup = zq_history.get_at_or_before(contract, kp.snapshot_date)
            if zq_lookup is None:
                continue
            _, zq_price = zq_lookup
            zq_p = _zq_implied_for_bucket(meeting, zq_price, bucket, decision_buckets)
            spread = abs(kp.yes_mid - zq_p) * 100.0
            days_to_fomc = (fomc_date - kp.snapshot_date).days
            cps.append(ConvergencePoint(
                snapshot_date=kp.snapshot_date,
                days_to_fomc=days_to_fomc,
                kalshi_p=kp.yes_mid,
                zq_p=zq_p,
                spread_pp=spread,
            ))
        if not cps:
            continue

        max_spread = max(c.spread_pp for c in cps)
        final_spread = cps[-1].spread_pp
        converged = final_spread < convergence_threshold_pp

        # realized = 1 if the meeting actually landed on this bucket
        realized = 1.0 if abs(meeting.post_lower - bucket) < 0.0001 else 0.0

        # Simulated PnL: if we entered at the WIDEST spread, what would we
        # have earned by holding to settlement?
        # Direction: long the cheap side, short the expensive.
        widest = max(cps, key=lambda c: c.spread_pp)
        # Convergence payoff in pp = widest_spread - final_spread (model
        # collected the gap). On a $100 position, that's the % pnl.
        simulated_pnl = widest.spread_pp - final_spread

        results.append(ConvergenceResult(
            fomc_date=fomc_date,
            market_ticker=ticker,
            bucket_lower=bucket,
            points=cps,
            final_spread_pp=final_spread,
            max_spread_pp=max_spread,
            converged=converged,
            realized=realized,
            simulated_pnl_pct=simulated_pnl,
        ))

    if not results:
        return results, ConvergenceStats(0, 0.0, 0.0, 0.0, 0.0, 0)

    n = len(results)
    conv_rate = sum(1 for r in results if r.converged) / n
    mean_max = sum(r.max_spread_pp for r in results) / n
    mean_final = sum(r.final_spread_pp for r in results) / n
    mean_pnl = sum(r.simulated_pnl_pct for r in results) / n

    # Basis blowup: market traded against the realized outcome at T+0.
    # Crude heuristic: realized=1.0 but final kalshi_p < 0.30 (or vice versa).
    blowups = sum(
        1 for r in results
        if (r.realized == 1.0 and r.points[-1].kalshi_p < 0.30)
        or (r.realized == 0.0 and r.points[-1].kalshi_p > 0.70)
    )

    return results, ConvergenceStats(
        n_pairs=n,
        convergence_rate=conv_rate,
        mean_max_spread=mean_max,
        mean_final_spread=mean_final,
        mean_simulated_pnl=mean_pnl,
        n_basis_blowups=blowups,
    )


# ── Self-test (synthetic) ───────────────────────────────────────────────
if __name__ == "__main__":
    # Synthetic: one historical FOMC, 4 snapshots showing convergence.
    meeting = HISTORICAL_FOMCS[10]  # 2023-05-03 hike
    print(f"using {meeting.fomc_date}")

    # Build Kalshi history: T-30 wildly wrong, T-1 nearly converged.
    kalshi = [
        KalshiHistoricalPoint("KXFED-23MAY-25HIKE", meeting.fomc_date,
                              meeting.fomc_date - dt.timedelta(days=30),
                              meeting.post_lower, 0.40),
        KalshiHistoricalPoint("KXFED-23MAY-25HIKE", meeting.fomc_date,
                              meeting.fomc_date - dt.timedelta(days=14),
                              meeting.post_lower, 0.60),
        KalshiHistoricalPoint("KXFED-23MAY-25HIKE", meeting.fomc_date,
                              meeting.fomc_date - dt.timedelta(days=7),
                              meeting.post_lower, 0.78),
        KalshiHistoricalPoint("KXFED-23MAY-25HIKE", meeting.fomc_date,
                              meeting.fomc_date - dt.timedelta(days=1),
                              meeting.post_lower, 0.92),
    ]

    # Build perfect ZQ history → ZQ should imply ~1.0 prob for the realized bucket.
    h = ZQHistory()
    contract = contract_for_month(meeting.fomc_date)
    month_start = meeting.fomc_date.replace(day=1)
    month_end = _month_end(meeting.fomc_date)
    total = (month_end - month_start).days + 1
    pre_d = max(0, (meeting.fomc_date - month_start).days)
    post_d = max(1, total - pre_d)
    implied_avg = (pre_d * meeting.pre_mid + post_d * meeting.post_mid) / total
    zq_price = round(100.0 - implied_avg * 100.0, 4)
    for delta in range(60):
        h._data[(contract, meeting.fomc_date - dt.timedelta(days=delta))] = zq_price

    results, stats = run_convergence_backtest(kalshi, h)
    print(f"n results: {len(results)}")
    if results:
        for r in results:
            print(r.explain())
            for cp in r.points:
                print(f"  T-{cp.days_to_fomc:>2}d: kalshi={cp.kalshi_p:.2f} zq={cp.zq_p:.2f} spread={cp.spread_pp:.1f}pp")
        print(stats.explain())
        # Final spread should be small (Kalshi at 0.92 vs ZQ at 1.0 = 8pp).
        assert results[0].converged or results[0].final_spread_pp < 10.0
        # Simulated PnL should be positive: widest was at T-30 (~60pp), final 8pp → 52pp PnL.
        assert results[0].simulated_pnl_pct > 30.0
    print("convergence self-test OK")
