"""FedWatch parity backtest — validate our pricing math against CME's published.

The decomposition backtest measures (our_prediction vs actual_outcome) which
mixes math-correctness with FOMC-surprise noise. This parity check isolates
math-correctness: same-day same-meeting our_p vs cme_p comparison.

Pass criteria:
    |our_p - cme_p| < 1pp on >=90% of comparison points.

If parity passes but decomposition fails → math is correct, market just got
surprised. If parity fails → bug in our pricing module.

Data source: CME FedWatch publishes implied probabilities daily. Free public
archive does not exist — operator populates CSV from any of:
  - CME FedWatch tool (manual screenshot/extract)
  - Bloomberg / Refinitiv (paid)
  - Investing.com FedWatch mirror (current snapshot only, no archive)
  - Wayback Machine snapshots of cmegroup.com/fedwatch

CSV format (data/historical/cme_fedwatch_history.csv):
    snapshot_date,fomc_date,target_lower,cme_prob
    2024-09-17,2024-09-18,0.0500,0.65    # P(target=5.00-5.25%) on T-1 to Sep FOMC
    2024-09-17,2024-09-18,0.0475,0.35
    2024-09-11,2024-09-18,0.0500,0.40
    ...
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .historical_fomc import HistoricalFOMC, HISTORICAL_FOMCS
from .zq_history import ZQHistory, contract_for_month
from .decomposition import buckets_for_meeting, _month_end
from ..pricing.fed_funds import FOMCContext, decision_probs

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FedWatchSnapshot:
    snapshot_date: dt.date
    fomc_date: dt.date
    target_lower: float    # decimal e.g. 0.0500 for 5.00-5.25% range
    cme_prob: float        # CME's published probability, [0, 1]


@dataclass
class ParityResult:
    snapshot_date: dt.date
    fomc_date: dt.date
    target_lower: float
    cme_prob: float
    our_prob: float
    abs_err_pp: float       # |our - cme| in percentage points (0-100)


def load_fedwatch_csv(path: Path) -> list[FedWatchSnapshot]:
    """Parse operator-populated CME FedWatch history CSV."""
    snaps: list[FedWatchSnapshot] = []
    if not path.exists():
        _log.warning(f"fedwatch parity CSV not found: {path}")
        return snaps
    with open(path) as f:
        # Skip lines starting with '#' (comments); DictReader doesn't filter
        clean = (line for line in f if not line.lstrip().startswith("#"))
        reader = csv.DictReader(clean)
        for row in reader:
            try:
                snaps.append(FedWatchSnapshot(
                    snapshot_date=dt.date.fromisoformat(row["snapshot_date"]),
                    fomc_date=dt.date.fromisoformat(row["fomc_date"]),
                    target_lower=float(row["target_lower"]),
                    cme_prob=float(row["cme_prob"]),
                ))
            except (KeyError, ValueError, TypeError) as e:
                _log.warning(f"skipping malformed row {row}: {e}")
    _log.info(f"loaded {len(snaps)} FedWatch snapshots from {path}")
    return snaps


def _find_fomc(fomc_date: dt.date) -> Optional[HistoricalFOMC]:
    for f in HISTORICAL_FOMCS:
        if f.fomc_date == fomc_date:
            return f
    return None


def compute_parity(
    snapshots: list[FedWatchSnapshot],
    zq_history: ZQHistory,
) -> list[ParityResult]:
    """For each snapshot, compute our_p from ZQ on snapshot_date and compare to cme_p."""
    results: list[ParityResult] = []
    for snap in snapshots:
        fomc = _find_fomc(snap.fomc_date)
        if fomc is None:
            _log.debug(f"no historical FOMC for {snap.fomc_date}")
            continue

        # Find ZQ settle on snapshot_date for the FOMC's contract month
        contract = contract_for_month(snap.fomc_date)
        found = zq_history.get_at_or_before(
            contract, snap.snapshot_date, max_lookback_days=7
        )
        if found is None:
            _log.debug(f"no ZQ data for {contract} at/before {snap.snapshot_date}")
            continue
        _zq_date, zq_settle = found

        # Run decomposition: ZQ → P(post-meeting target)
        ctx = FOMCContext(
            current_target_lower=fomc.pre_lower,
            current_target_upper=fomc.pre_upper,
            fomc_date=snap.fomc_date,
            contract_month_start=snap.fomc_date.replace(day=1),
            contract_month_end=_month_end(snap.fomc_date),
            decision_buckets=buckets_for_meeting(fomc),
        )
        probs = decision_probs(zq_settle, ctx)

        # Our prob for the target_lower bucket
        our_p = probs.get(snap.target_lower, 0.0)
        err_pp = abs(our_p - snap.cme_prob) * 100.0

        results.append(ParityResult(
            snapshot_date=snap.snapshot_date,
            fomc_date=snap.fomc_date,
            target_lower=snap.target_lower,
            cme_prob=snap.cme_prob,
            our_prob=our_p,
            abs_err_pp=err_pp,
        ))
    return results


def evaluate_gate(results: list[ParityResult], threshold_pp: float = 1.0) -> dict:
    """Pass iff >=90% of comparison points have |our - cme| < threshold_pp."""
    n = len(results)
    if n == 0:
        return {"n": 0, "pct_within": 0.0, "passes": False, "threshold_pp": threshold_pp}
    within = sum(1 for r in results if r.abs_err_pp < threshold_pp)
    pct = 100.0 * within / n
    mae_pp = sum(r.abs_err_pp for r in results) / n
    max_err_pp = max(r.abs_err_pp for r in results)
    return {
        "n": n,
        "within_n": within,
        "pct_within": round(pct, 2),
        "mae_pp": round(mae_pp, 3),
        "max_err_pp": round(max_err_pp, 3),
        "threshold_pp": threshold_pp,
        "passes": pct >= 90.0,
    }


# === Self-test ===
if __name__ == "__main__":
    import io
    # Build a synthetic snapshot dataset where our math equals CME's exactly.
    csv_text = """snapshot_date,fomc_date,target_lower,cme_prob
2022-03-15,2022-03-16,0.0025,1.000
2022-03-15,2022-03-16,0.0050,0.000
2022-05-03,2022-05-04,0.0050,0.000
2022-05-03,2022-05-04,0.0075,1.000
"""
    # Parse
    snaps = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        snaps.append(FedWatchSnapshot(
            snapshot_date=dt.date.fromisoformat(row["snapshot_date"]),
            fomc_date=dt.date.fromisoformat(row["fomc_date"]),
            target_lower=float(row["target_lower"]),
            cme_prob=float(row["cme_prob"]),
        ))
    print(f"synth snapshots: {len(snaps)}")

    # Build a perfect ZQ history (time-weighted implied avg = 100% P(actual))
    h = ZQHistory()
    for f in HISTORICAL_FOMCS:
        c = contract_for_month(f.fomc_date)
        month_start = f.fomc_date.replace(day=1)
        month_end = _month_end(f.fomc_date)
        total = (month_end - month_start).days + 1
        pre_d = max(0, (f.fomc_date - month_start).days)
        post_d = max(1, total - pre_d)
        implied_avg = (pre_d * f.pre_mid + post_d * f.post_mid) / total
        zq_price = round(100.0 - implied_avg * 100.0, 4)
        h._data[(c, f.fomc_date - dt.timedelta(days=1))] = zq_price

    results = compute_parity(snaps, h)
    print(f"parity results: {len(results)}")
    for r in results:
        print(f"  {r.fomc_date} target={r.target_lower:.4f} cme={r.cme_prob:.3f} "
              f"our={r.our_prob:.3f} err={r.abs_err_pp:.2f}pp")

    gate = evaluate_gate(results, threshold_pp=1.0)
    print(f"\ngate: {gate}")
    print("fedwatch_parity self-test OK" if gate["passes"] else
          "fedwatch_parity self-test: gate FAILED on synthetic (math diverges from itself!)")
