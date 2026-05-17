"""Historical ZQ (30-day Fed Funds) futures daily settlement loader.

CSV format:
    contract,date,settlement
    ZQF24,2023-12-15,94.6700
    ZQF24,2023-12-18,94.6750
    ...

Source for the operator: https://www.cmegroup.com/markets/interest-rates/stirs/30-day-federal-fund.quotes.html
or CME DataMine (paid). For backtest, manually export historical settles
or scrape from CME's public daily settlement archive.

CME ZQ contract month codes:
    F=Jan G=Feb H=Mar J=Apr K=May M=Jun
    N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec
Year is two digits.

This module is read-only: load history once at backtest start, query by
(contract, date). Operator drops the CSV in data/historical/zq_history.csv.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


CONTRACT_MONTH_CODES = {
    1:  "F", 2: "G", 3: "H", 4:  "J", 5:  "K", 6:  "M",
    7:  "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}


def contract_for_month(d: dt.date) -> str:
    """ZQ contract code for the month containing date d. e.g. dt.date(2024,9,18) → ZQU24."""
    return f"ZQ{CONTRACT_MONTH_CODES[d.month]}{d.year % 100:02d}"


class ZQHistory:
    """Lookup table: (contract, date) → settlement_price."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, dt.date], float] = {}

    @classmethod
    def load_csv(cls, path: Path) -> "ZQHistory":
        h = cls()
        if not path.exists():
            _log.warning(f"ZQ history CSV not found: {path}")
            return h
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    h._data[(row["contract"], dt.date.fromisoformat(row["date"]))] = float(row["settlement"])
                except (KeyError, ValueError) as e:
                    _log.warning(f"skip bad row: {row} ({e})")
        _log.info(f"loaded {len(h._data)} ZQ settles from {path}")
        return h

    def get(self, contract: str, date: dt.date) -> Optional[float]:
        return self._data.get((contract, date))

    def get_at_or_before(
        self, contract: str, target: dt.date, max_lookback_days: int = 7,
    ) -> Optional[tuple[dt.date, float]]:
        """Return the most recent settlement on or before target_date.

        Walks back up to max_lookback_days to skip weekends/holidays.
        """
        for delta in range(max_lookback_days + 1):
            d = target - dt.timedelta(days=delta)
            v = self._data.get((contract, d))
            if v is not None:
                return d, v
        return None

    def __len__(self) -> int:
        return len(self._data)


# ── Self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    assert contract_for_month(dt.date(2024, 9, 18)) == "ZQU24"
    assert contract_for_month(dt.date(2026, 6, 17)) == "ZQM26"
    assert contract_for_month(dt.date(2022, 12, 14)) == "ZQZ22"

    # In-memory-only test (no CSV).
    h = ZQHistory()
    h._data[("ZQU24", dt.date(2024, 9, 17))] = 94.6850
    h._data[("ZQU24", dt.date(2024, 9, 18))] = 94.7250
    assert h.get("ZQU24", dt.date(2024, 9, 18)) == 94.7250
    assert h.get("ZQU24", dt.date(2024, 9, 19)) is None

    # at-or-before walks weekends.
    h._data[("ZQU24", dt.date(2024, 9, 13))] = 94.6800  # Friday
    found = h.get_at_or_before("ZQU24", dt.date(2024, 9, 15))  # Sunday
    assert found is not None
    assert found[0] == dt.date(2024, 9, 13)
    print("zq_history self-test OK")
