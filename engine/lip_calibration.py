"""Per-series multiplier from paid/estimate ratios, shrunk toward 1.

The prior is 1: with no matched credits the selector and the sizer use the
unscaled LIP estimate. Each series' sample mean of paid/estimate is pulled
toward 1 by ``strength`` pseudo-observations (default 5, the same strength
the compounding allocator uses for its own priors). Model output is not an
observation. Callers pass rows that ``lip_reconcile`` matched to a tagged
credit.

This is not a fit to any historical statement. April and May 2026 payouts
were earned under the 28 February 2026 rules and are not inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from mm.compound import PRIOR_STRENGTH, posterior

PRIOR_FACTOR = 1.0


@dataclass(frozen=True)
class RatioObs:
    series: str
    estimated_usd: Decimal
    paid_usd: Decimal

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
    """Multiplicative correction keyed by series. Missing series stay at 1."""
    grouped: dict[str, list[float]] = {}
    for obs in observations:
        ratio = obs.ratio
        if ratio is None:
            continue
        grouped.setdefault(obs.series, []).append(float(ratio))
    factors: dict[str, float] = {}
    for series, ratios in grouped.items():
        mean = sum(ratios) / len(ratios)
        factors[series] = posterior(mean, PRIOR_FACTOR, len(ratios), strength=strength)
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
