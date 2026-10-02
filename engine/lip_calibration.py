"""Per-series multiplier from paid/estimate ratios, shrunk toward 1.

The prior is 1: with no matched credits the selector and the sizer use the
unscaled LIP estimate. Each series' observed factor is the RATIO OF SUMS
(sum paid / sum estimated over its rows), clamped to [0, 2], then pulled
toward 1 by ``strength`` pseudo-observations (default 5, the same strength
the compounding allocator uses for its own priors) with n = row count. A
mean of per-row ratios let one tiny estimate dominate (paid $12 on a $1
estimate next to $30 on $40 gave 6.4 instead of 1.02). Model output is not
an observation. Callers pass rows that ``lip_reconcile`` matched to a
tagged credit.

This is not a fit to any historical statement. April and May 2026 payouts
were earned under the 28 February 2026 rules and are not inputs.

``inferred=True`` marks a residual the balance reconciler attributed.
Those rows are EXCLUDED from ``series_factors``: the residual is split
pro-rata to our own estimates (no per-series information), so every
series would get the same ratio. They are not ``PAID_SOURCES`` credits;
``error_distribution`` still reports them (flagged).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from mm.compound import PRIOR_STRENGTH, posterior

PRIOR_FACTOR = 1.0
FACTOR_MIN = 0.0
FACTOR_MAX = 2.0


@dataclass(frozen=True)
class RatioObs:
    series: str
    estimated_usd: Decimal
    paid_usd: Decimal
    inferred: bool = False

    @property
    def ratio(self) -> Decimal | None:
        if self.estimated_usd <= 0:
            return None
        return self.paid_usd / self.estimated_usd

    @property
    def error_usd(self) -> Decimal:
        return self.paid_usd - self.estimated_usd


def series_factors(observations: list[RatioObs], *,
                   strength: int = PRIOR_STRENGTH) -> dict[str, float]:
    """Multiplicative correction keyed by series. Missing series stay at 1.

    Statement rows only (``inferred`` rows are skipped); per series the
    clamped ratio of sums, shrunk toward 1 by ``strength``."""
    grouped: dict[str, list[Decimal]] = {}
    for obs in observations:
        if obs.inferred or obs.estimated_usd <= 0:
            continue
        row = grouped.setdefault(obs.series, [Decimal(0), Decimal(0), Decimal(0)])
        row[0] += obs.paid_usd
        row[1] += obs.estimated_usd
        row[2] += 1
    factors: dict[str, float] = {}
    for series, (paid, est, n) in grouped.items():
        ratio = min(FACTOR_MAX, max(FACTOR_MIN, float(paid / est)))
        factors[series] = posterior(ratio, PRIOR_FACTOR, int(n), strength=strength)
    return factors


def error_distribution(observations: list[RatioObs]) -> dict:
    """Count, median paid/estimate, and mean absolute dollar error."""
    ratios = [float(obs.ratio) for obs in observations if obs.ratio is not None]
    errors = [abs(float(obs.error_usd)) for obs in observations]
    rows = [
        {
            "series": obs.series,
            "estimated_usd": format(obs.estimated_usd, "f"),
            "paid_usd": format(obs.paid_usd, "f"),
            "ratio": None if obs.ratio is None else format(obs.ratio, "f"),
            "error_usd": format(obs.error_usd, "f"),
            "inferred": bool(obs.inferred),
        }
        for obs in observations
    ]
    return {
        "count": len(observations),
        "ratio_count": len(ratios),
        "median_ratio": None if not ratios else float(median(ratios)),
        "mean_abs_error_usd": None if not errors else sum(errors) / len(errors),
        "rows": rows,
    }
