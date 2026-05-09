"""FedWatch decomposition backtest — validates the pricing model itself.

For each historical FOMC, replay the model at multiple lookback windows
(T-30, T-7, T-1) using the ZQ daily settle on those days. Compare predicted
bucket probabilities to the realized one-hot outcome.

Outputs per-meeting + aggregate statistics:

  per_meeting:
    fomc_date | lookback | predicted_top_bucket | predicted_top_p
              | realized_bucket | abs_error_pp_top | brier_score | hit

  aggregate:
    mean abs error (pp)
    top-pick hit rate (%)
    Brier score (lower = better)
    calibration curve (predicted vs realized in 0.1-buckets)

INTERPRETATION:
  - Hit rate ≥80% at T-1: model is reliable for last-week trades.
  - MAE ≤10pp at T-7: model is reliable for week-ahead trades.
  - MAE ≤20pp at T-30: model has month-ahead signal.
  - Calibration: when model says 70%, real frequency should be ~70%.

If decomposition validates, the only remaining basis risk in live trading
is "Kalshi mid != FedWatch implied" — typically 2-5pp drift, much smaller
than the dislocations we're hunting (8-25pp).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Optional

from ..pricing.fed_funds import FOMCContext, decision_probs
from .historical_fomc import HistoricalFOMC, realized_one_hot
from .zq_history import ZQHistory, contract_for_month

_log = logging.getLogger(__name__)


@dataclass
class MeetingResult:
    fomc_date:           dt.date
    lookback_days:       int
    snapshot_date:       dt.date
    zq_contract:         str
    zq_price:            float
    predicted_probs:     dict[float, float]
    realized:            dict[float, float]   # one-hot
    realized_bucket:     float
    top_predicted:       float
    top_predicted_p:     float
    abs_error_top_pp:    float                # |1 - p_top| if top hit, else top_p
    brier_score:         float                # sum((p_i - one_hot_i)^2)
    hit:                 bool                 # top prediction == realized

    def explain(self) -> dict:
        return {
            "fomc":             self.fomc_date.isoformat(),
            "lookback_d":       self.lookback_days,
            "zq":               f"{self.zq_contract}={self.zq_price}",
            "top_pred":         f"{self.top_predicted*100:.2f}% @ {self.top_predicted_p:.2f}",
            "realized":         f"{self.realized_bucket*100:.2f}%",
            "abs_err_top_pp":   round(self.abs_error_top_pp, 2),
            "brier":            round(self.brier_score, 4),
            "hit":              self.hit,
        }


@dataclass
class BacktestStats:
    n_meetings:        int
    lookback_days:     int
    hit_rate:          float
    mean_abs_err_pp:   float
    median_abs_err_pp: float
    mean_brier:        float
    calibration:       dict[str, tuple[float, int]]  # bin_label → (avg_realized, count)

    def explain(self) -> dict:
        return {
            "n_meetings":       self.n_meetings,
            "lookback_d":       self.lookback_days,
            "hit_rate_%":       round(self.hit_rate * 100, 1),
            "mean_abs_err_pp":  round(self.mean_abs_err_pp, 2),
            "median_err_pp":    round(self.median_abs_err_pp, 2),
            "brier":            round(self.mean_brier, 4),
            "calibration":      {k: (round(v[0], 3), v[1]) for k, v in self.calibration.items()},
        }


# ── Per-meeting replay ──────────────────────────────────────────────────
def replay_meeting(
    meeting:        HistoricalFOMC,
    zq_history:     ZQHistory,
    *,
    lookback_days:  int,
    decision_buckets: list[float],
) -> Optional[MeetingResult]:
    """Compute model prediction at T-lookback days before fomc_date."""
    target_date = meeting.fomc_date - dt.timedelta(days=lookback_days)
    contract = contract_for_month(meeting.fomc_date)
    found = zq_history.get_at_or_before(contract, target_date, max_lookback_days=7)
    if found is None:
        _log.debug(
            f"no ZQ data for {contract} at/before {target_date.isoformat()} "
            f"(skipping {meeting.fomc_date} T-{lookback_days})"
        )
        return None
    snapshot_date, zq_price = found

    ctx = FOMCContext(
        current_target_lower=meeting.pre_lower,
        current_target_upper=meeting.pre_upper,
        fomc_date=meeting.fomc_date,
        contract_month_start=meeting.fomc_date.replace(day=1),
        contract_month_end=_month_end(meeting.fomc_date),
        decision_buckets=decision_buckets,
    )
    predicted = decision_probs(zq_price, ctx)
    realized  = realized_one_hot(meeting, decision_buckets)

    top_pred = max(predicted, key=lambda b: predicted[b])
    top_pred_p = predicted[top_pred]
    realized_bucket = max(realized, key=lambda b: realized[b])

    # |error| on the top-predicted bucket vs its realized one-hot value.
    abs_err_top_pp = abs(predicted[top_pred] - realized[top_pred]) * 100.0

    # Brier across all buckets.
    brier = sum((predicted[b] - realized[b]) ** 2 for b in decision_buckets)

    return MeetingResult(
        fomc_date=meeting.fomc_date,
        lookback_days=lookback_days,
        snapshot_date=snapshot_date,
        zq_contract=contract,
        zq_price=zq_price,
        predicted_probs=predicted,
        realized=realized,
        realized_bucket=realized_bucket,
        top_predicted=top_pred,
        top_predicted_p=top_pred_p,
        abs_error_top_pp=abs_err_top_pp,
        brier_score=brier,
        hit=(top_pred == realized_bucket),
    )


def _month_end(d: dt.date) -> dt.date:
    if d.month == 12:
        return dt.date(d.year, 12, 31)
    nxt = dt.date(d.year, d.month + 1, 1)
    return nxt - dt.timedelta(days=1)


# ── Bucket auto-derivation ──────────────────────────────────────────────
def buckets_for_meeting(meeting: HistoricalFOMC) -> list[float]:
    """Reasonable bucket grid centered on pre-meeting target.

    Spans cut-50bp through hike-50bp in 25bp steps. Adjust if a specific
    meeting had wider expected-decision range.
    """
    base_lower = meeting.pre_lower
    return [
        round(base_lower - 0.0050, 4),
        round(base_lower - 0.0025, 4),
        round(base_lower,          4),
        round(base_lower + 0.0025, 4),
        round(base_lower + 0.0050, 4),
    ]


# ── Aggregate ───────────────────────────────────────────────────────────
def aggregate(results: list[MeetingResult]) -> BacktestStats:
    if not results:
        return BacktestStats(0, 0, 0.0, 0.0, 0.0, 0.0, {})
    hits = [r.hit for r in results]
    errs = sorted(r.abs_error_top_pp for r in results)
    briers = [r.brier_score for r in results]

    hit_rate = sum(hits) / len(hits)
    mean_err = sum(errs) / len(errs)
    median_err = errs[len(errs) // 2]
    mean_brier = sum(briers) / len(briers)

    # Calibration: bin top-predicted-p in 0.1 increments.
    bins: dict[str, list[bool]] = {}
    for r in results:
        bin_lo = math.floor(r.top_predicted_p * 10) / 10
        bin_hi = bin_lo + 0.1
        label = f"[{bin_lo:.1f},{bin_hi:.1f})"
        bins.setdefault(label, []).append(r.hit)
    calibration = {
        label: (sum(hits) / len(hits), len(hits)) for label, hits in sorted(bins.items())
    }

    lookback = results[0].lookback_days  # assume all same lookback

    return BacktestStats(
        n_meetings=len(results),
        lookback_days=lookback,
        hit_rate=hit_rate,
        mean_abs_err_pp=mean_err,
        median_abs_err_pp=median_err,
        mean_brier=mean_brier,
        calibration=calibration,
    )


def run_backtest(
    meetings:       list[HistoricalFOMC],
    zq_history:     ZQHistory,
    *,
    lookback_days:  int,
) -> tuple[list[MeetingResult], BacktestStats]:
    out: list[MeetingResult] = []
    for m in meetings:
        buckets = buckets_for_meeting(m)
        r = replay_meeting(m, zq_history, lookback_days=lookback_days, decision_buckets=buckets)
        if r is not None:
            out.append(r)
    return out, aggregate(out)


# ── Self-test (synthetic) ───────────────────────────────────────────────
if __name__ == "__main__":
    from .historical_fomc import HISTORICAL_FOMCS

    # Build a synthetic ZQ history that is "correct" — i.e. it always
    # implies an avg rate matching the realized post-meeting rate. The
    # backtest should then show very high hit rate, low MAE.
    h = ZQHistory()
    for m in HISTORICAL_FOMCS:
        contract = contract_for_month(m.fomc_date)
        # The "perfect" implied avg = weighted avg of pre + post days at
        # respective rates.
        month_start = m.fomc_date.replace(day=1)
        month_end = _month_end(m.fomc_date)
        total = (month_end - month_start).days + 1
        pre_d = max(0, (m.fomc_date - month_start).days)
        post_d = max(1, total - pre_d)
        implied_avg = (pre_d * m.pre_mid + post_d * m.post_mid) / total
        zq_price = round(100.0 - implied_avg * 100.0, 4)
        # Provide settlements for the 60 days before each FOMC.
        for delta in range(60):
            d = m.fomc_date - dt.timedelta(days=delta)
            h._data[(contract, d)] = zq_price
        # Also for nearby contracts (so the lookup doesn't miss).
    # Run T-1, T-7, T-30 backtests.
    for lb in (1, 7, 30):
        results, stats = run_backtest(HISTORICAL_FOMCS, h, lookback_days=lb)
        print(f"\nT-{lb} backtest: {stats.explain()}")
        # With perfect ZQ data, hit rate should be very high.
        assert stats.hit_rate > 0.85, f"perfect ZQ should hit >85%, got {stats.hit_rate}"
    print("\ndecomposition self-test OK")
