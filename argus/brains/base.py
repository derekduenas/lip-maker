"""DomainBrain abstract base + Prediction value object.

A brain is a pure function:  market dict  ->  Prediction
- Reads its data sources fresh on each call (or via cached adapters)
- Returns None if it cannot evaluate (missing data, edge case)
- Provides key_features dict for explainability + audit

This file has no Kalshi/venue dependencies — pure model interface.
"""
from __future__ import annotations

import abc
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass
class Prediction:
    """One brain's prediction for one market."""
    p_yes:           float            # in [0, 1]
    confidence:      float            # in [0, 1] — brain self-confidence
    key_features:    dict             # for explainability + audit
    brain_id:        str
    market_ticker:   str
    computed_at:     dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )

    def __post_init__(self) -> None:
        if not 0.0 <= self.p_yes <= 1.0:
            raise ValueError(f"p_yes={self.p_yes} not in [0, 1]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence={self.confidence} not in [0, 1]")


class DomainBrain(abc.ABC):
    """One domain's predictive model."""
    brain_id:       str = ""    # e.g. "music"
    market_pattern: str = ""    # e.g. "KXRANKLISTSONGSPOT*" (informational)

    @abc.abstractmethod
    def can_evaluate(self, market_ticker: str) -> bool:
        """Does this brain claim responsibility for this market?"""
        raise NotImplementedError

    @abc.abstractmethod
    def predict(self, market: dict) -> Optional[Prediction]:
        """Return Prediction or None if data is insufficient."""
        raise NotImplementedError

    def edge_vs_market(self, prediction: Prediction, market_p: float) -> float:
        """Signed edge in probability space: ours - theirs."""
        return prediction.p_yes - market_p


def confidence_from_extremes(p: float) -> float:
    """Confidence proxy: 1 - 4*p*(1-p). High at extremes, 0 at p=0.5."""
    return max(0.0, 1.0 - 4.0 * p * (1.0 - p))


if __name__ == "__main__":
    try:
        DomainBrain()  # type: ignore[abstract]
        print("FAIL: should have raised TypeError")
    except TypeError:
        print("OK: DomainBrain enforces abstract methods")

    p = Prediction(p_yes=0.7, confidence=0.8, key_features={"x": 1},
                   brain_id="test", market_ticker="X")
    print(f"OK: {p}")
    assert confidence_from_extremes(0.5) == 0.0
    assert confidence_from_extremes(0.95) > 0.8
    print("OK: confidence_from_extremes")
