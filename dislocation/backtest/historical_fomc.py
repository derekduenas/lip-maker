"""Historical FOMC dates + decisions — public-record data.

Each row: (fomc_date, pre_meeting_lower, pre_meeting_upper, post_meeting_lower)
where rates are decimal (e.g., 0.0525 = 5.25%) and lower bound is the
lower bound of the target range (Fed quotes ranges since 2008).

Sources:
  - https://www.federalreserve.gov/monetarypolicy/openmarket.htm
  - Press releases for each meeting
  - Wikipedia "Federal funds rate" rate-history table

Updates: append new FOMCs as they occur. Run dislocation_backtest after
each new entry to refresh the validation stats.

NOTE: This is a STARTER table. Operator should verify and extend before
relying on backtest results. Audit-driven trust, not model-driven.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class HistoricalFOMC:
    fomc_date:           dt.date
    pre_lower:           float  # decimal, e.g. 0.0525
    pre_upper:           float  # decimal, e.g. 0.0550
    post_lower:          float  # decimal, e.g. 0.0500 (after the cut)
    move_bp:             int    # signed, e.g. -25 for cut, +25 for hike, 0 hold

    @property
    def post_upper(self) -> float:
        return self.post_lower + 0.0025

    @property
    def pre_mid(self) -> float:
        return (self.pre_lower + self.pre_upper) / 2.0

    @property
    def post_mid(self) -> float:
        return (self.post_lower + self.post_upper) / 2.0


# 2022-2024: aggressive hiking cycle then plateau.
# Verify against the Fed's openmarket.htm page before relying on these.
HISTORICAL_FOMCS: list[HistoricalFOMC] = [
    # 2022
    HistoricalFOMC(dt.date(2022, 1, 26), 0.0000, 0.0025, 0.0000, 0),
    HistoricalFOMC(dt.date(2022, 3, 16), 0.0000, 0.0025, 0.0025, +25),
    HistoricalFOMC(dt.date(2022, 5, 4),  0.0025, 0.0050, 0.0075, +50),
    HistoricalFOMC(dt.date(2022, 6, 15), 0.0075, 0.0100, 0.0150, +75),
    HistoricalFOMC(dt.date(2022, 7, 27), 0.0150, 0.0175, 0.0225, +75),
    HistoricalFOMC(dt.date(2022, 9, 21), 0.0225, 0.0250, 0.0300, +75),
    HistoricalFOMC(dt.date(2022, 11, 2), 0.0300, 0.0325, 0.0375, +75),
    HistoricalFOMC(dt.date(2022, 12, 14), 0.0375, 0.0400, 0.0425, +50),
    # 2023
    HistoricalFOMC(dt.date(2023, 2, 1),  0.0425, 0.0450, 0.0450, +25),
    HistoricalFOMC(dt.date(2023, 3, 22), 0.0450, 0.0475, 0.0475, +25),
    HistoricalFOMC(dt.date(2023, 5, 3),  0.0475, 0.0500, 0.0500, +25),
    HistoricalFOMC(dt.date(2023, 6, 14), 0.0500, 0.0525, 0.0500, 0),
    HistoricalFOMC(dt.date(2023, 7, 26), 0.0500, 0.0525, 0.0525, +25),
    HistoricalFOMC(dt.date(2023, 9, 20), 0.0525, 0.0550, 0.0525, 0),
    HistoricalFOMC(dt.date(2023, 11, 1), 0.0525, 0.0550, 0.0525, 0),
    HistoricalFOMC(dt.date(2023, 12, 13), 0.0525, 0.0550, 0.0525, 0),
    # 2024 — holds, then cuts begin
    HistoricalFOMC(dt.date(2024, 1, 31), 0.0525, 0.0550, 0.0525, 0),
    HistoricalFOMC(dt.date(2024, 3, 20), 0.0525, 0.0550, 0.0525, 0),
    HistoricalFOMC(dt.date(2024, 5, 1),  0.0525, 0.0550, 0.0525, 0),
    HistoricalFOMC(dt.date(2024, 6, 12), 0.0525, 0.0550, 0.0525, 0),
    HistoricalFOMC(dt.date(2024, 7, 31), 0.0525, 0.0550, 0.0525, 0),
    HistoricalFOMC(dt.date(2024, 9, 18), 0.0525, 0.0550, 0.0475, -50),
    HistoricalFOMC(dt.date(2024, 11, 7), 0.0475, 0.0500, 0.0450, -25),
    HistoricalFOMC(dt.date(2024, 12, 18), 0.0450, 0.0475, 0.0425, -25),
    # 2025 — operator extends as meetings happen.
    # HistoricalFOMC(dt.date(2025, 1, 29), 0.0425, 0.0450, 0.0425, 0),
    # ...
]


def fomcs_in_range(start: dt.date, end: dt.date) -> list[HistoricalFOMC]:
    return [m for m in HISTORICAL_FOMCS if start <= m.fomc_date <= end]


def get_fomc(d: dt.date) -> Optional[HistoricalFOMC]:
    for m in HISTORICAL_FOMCS:
        if m.fomc_date == d:
            return m
    return None


def realized_one_hot(meeting: HistoricalFOMC, buckets: list[float]) -> dict[float, float]:
    """One-hot vector across buckets for the realized outcome.

    Snaps the realized post_lower to the closest bucket lower-bound.
    """
    nearest = min(buckets, key=lambda b: abs(b - meeting.post_lower))
    return {b: (1.0 if b == nearest else 0.0) for b in buckets}


# ── Self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    assert len(HISTORICAL_FOMCS) >= 24, "should have at least 2022-2024 meetings"
    h = get_fomc(dt.date(2024, 9, 18))
    assert h is not None
    assert h.move_bp == -50
    assert h.post_lower == 0.0475

    buckets = [0.0500, 0.0525, 0.0550]
    oh = realized_one_hot(HISTORICAL_FOMCS[0], buckets)
    assert sum(oh.values()) == 1.0

    rng = fomcs_in_range(dt.date(2022, 1, 1), dt.date(2022, 12, 31))
    assert len(rng) == 8, f"expected 8 meetings in 2022, got {len(rng)}"
    print(f"historical_fomc self-test OK ({len(HISTORICAL_FOMCS)} meetings loaded)")
