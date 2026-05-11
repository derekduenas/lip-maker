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

    def passes_gate(self, bss_gate: float, min_n: int) -> bool:
        """Live-graduation gate: model BSS >= bss_gate AND n_test >= min_n.

        2026-05-10 (Phase 3): switched from raw Brier to BSS — naive
        base-rate predictor passes Brier 0.20 trivially under heavy class
        imbalance, so raw-Brier gate gives false confidence.
        """
        if self.test_brier is None:
            return False
        return self.test_brier.skill >= bss_gate and self.n_test >= min_n


def run_backtest(brain: DomainBrain, settled_markets: list[dict]) -> BacktestResult:
    """Phase 1 skeleton. Phase 4 fills in feature extraction + outcomes."""
    raise NotImplementedError("Phase 4 implementation pending")
