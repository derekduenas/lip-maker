"""ForecastEx placeholder.

IBKR NTM 2026-139 opened a market-maker program to customers of members
effective 20 May 2026, and NTM 2026-186 / 2026-191 describe a liquidity
retainer effective 21 July 2026 (amended 23 July 2026). The retainer
exhibit is not in this repo, and there is no public retail order API to
call. This adapter exists so the venue enum and the risk engine can name
the venue. Every write returns ``not_available``.
"""
from __future__ import annotations

from mm.types import VenueName


class ForecastExAdapter:
    name = VenueName.FORECASTEX
    available = False
    reason = (
        "ForecastEx MM access is a member-customer program (IBKR NTM 2026-139, "
        "retainer NTM 2026-186). No order API is wired until that access exists."
    )

    def __init__(self, *args, **kwargs) -> None:
        self.paper = True

    def place(self, *args, **kwargs) -> dict:
        return {"ok": False, "error": "not_available", "detail": self.reason}

    def cancel_all(self, *args, **kwargs) -> dict:
        return {"ok": False, "error": "not_available", "detail": self.reason}


def get_adapter(name: str, **kwargs):
    from mm.venues.kalshi import KalshiAdapter
    from mm.venues.pmus import PMUSAdapter
    key = str(name).lower()
    if key in ("kalshi", VenueName.KALSHI.value):
        return KalshiAdapter(**kwargs)
    if key in ("pmus", "polymarket", "polymarket_us", VenueName.PMUS.value):
        return PMUSAdapter(**kwargs)
    if key in ("forecastex", VenueName.FORECASTEX.value):
        return ForecastExAdapter(**kwargs)
    raise KeyError(key)
