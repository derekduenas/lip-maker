"""Backtest harness skeleton — Phase 4 fills out the data + scoring loops."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from argus.brains.base import DomainBrain
from argus.scoring.brier import BrierResult

_log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    brain_id:    str
    n_train:     int
    n_test:      int
    train_brier: Optional[BrierResult]
    test_brier:  Optional[BrierResult]
    calibration: list[dict] = field(default_factory=list)
    notes:       list[str]  = field(default_factory=list)

    def passes_gate(self, gate: float, min_n: int) -> bool:
        if self.test_brier is None:
            return False
        return self.test_brier.brier <= gate and self.n_test >= min_n


def run_backtest(brain: DomainBrain, settled_markets: list[dict]) -> BacktestResult:
    """Phase 1 skeleton. Phase 4 fills in feature extraction + outcomes."""
    raise NotImplementedError("Phase 4 implementation pending")
