"""Unified risk checks. Fail closed. No runtime override.

Layers, worst-case dollars (contracts × price paid, supplied by the caller
as ``add_usd`` / inventory marks):

    market → series → underlying → venue → account gross

A daily-loss breach, a fill-rate breach, or a disconnect longer than the
threshold latches a kill. The kill means cancel every resting order. The
engine does not send the cancels itself; ``killed`` tells the venue loop to.

``MAX_FILLS_PER_MINUTE`` (config/constitution.py) is enforced here. The
clock latches: it does not quietly re-arm when the minute rolls over.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from config import constitution
from mm.bankroll import capital_usd
from mm.fair_value import family_for_series, series_of

ZERO = Decimal("0")
DISCONNECT_PULL_SEC = 3.0


class FillClock:
    def __init__(self, limit: int = constitution.MAX_FILLS_PER_MINUTE,
                 window_sec: float = 60.0) -> None:
        self.limit = int(limit)
        self.window_sec = float(window_sec)
        self._ts: deque[float] = deque()
        self.halted = False
        self.reason = ""

    def record(self, n: int = 1, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for _ in range(max(0, int(n))):
            self._ts.append(now)
        self.check(now)

    def check(self, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now
        if self.halted:
            return False, self.reason
        while self._ts and now - self._ts[0] >= self.window_sec:
            self._ts.popleft()
        if len(self._ts) >= self.limit:
            self.halted = True
            self.reason = (
                f"fills_per_minute: {len(self._ts)} >= {self.limit} "
                f"in {self.window_sec:.0f}s — halt, cancel all"
            )
            return False, self.reason
        return True, ""

    def reset(self) -> None:
        self._ts.clear()
        self.halted = False
        self.reason = ""


# Process-wide. Sentinel and the paper runner share it so the constitution
# constant is enforced in one place.
FILL_CLOCK = FillClock()


@dataclass
class Limits:
    capital_usd: Decimal
    daily_loss_usd: Decimal
    per_market_usd: Decimal
    per_series_usd: Decimal
    per_underlying_usd: Decimal
    per_venue_usd: Decimal
    gross_usd: Decimal

    @staticmethod
    def from_capital(capital: Decimal | None = None, *,
                     ramp: int | None = None) -> "Limits":
        cap = capital if capital is not None else capital_usd()
        # Small live ($500–$1k): tighter than the ramp-4 paper constitution.
        if cap <= Decimal("1000"):
            daily = min(Decimal("40"), cap * Decimal("0.05"))
            return Limits(
                capital_usd=cap,
                daily_loss_usd=daily,
                per_market_usd=min(Decimal("50"), cap * Decimal("0.10")),
                per_series_usd=min(Decimal("100"), cap * Decimal("0.20")),
                per_underlying_usd=min(Decimal("150"), cap * Decimal("0.30")),
                per_venue_usd=cap * Decimal(str(constitution.MAX_PER_HEDGE_VENUE_PCT)),
                gross_usd=cap * Decimal(str(constitution.MAX_GROSS_EXPOSURE_PCT)),
            )
        ramp_cap = Decimal(str(constitution.MAX_DAILY_LOSS_BY_RAMP.get(
            int(ramp or 4), 250.0)))
        daily = min(cap * Decimal("0.05"), ramp_cap)
        return Limits(
            capital_usd=cap,
            daily_loss_usd=daily,
            per_market_usd=cap * Decimal(str(constitution.MAX_PER_MARKET_PCT)),
            per_series_usd=cap * Decimal(str(constitution.MAX_PER_SERIES_PCT)),
            per_underlying_usd=cap * Decimal("0.25"),
            per_venue_usd=cap * Decimal(str(constitution.MAX_PER_HEDGE_VENUE_PCT)),
            gross_usd=cap * Decimal(str(constitution.MAX_GROSS_EXPOSURE_PCT)),
        )


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    cancel_all: bool = False


@dataclass
class RiskEngine:
    limits: Limits = field(default_factory=Limits.from_capital)
    clock: FillClock = field(default_factory=lambda: FILL_CLOCK)
    killed: bool = False
    kill_reason: str = ""
    # market -> worst-case USD already on
    market_usd: dict = field(default_factory=dict)
    venue_usd: dict = field(default_factory=dict)

    def _kill(self, reason: str) -> RiskDecision:
        self.killed = True
        self.kill_reason = reason
        return RiskDecision(False, reason, cancel_all=True)

    def record_fill(self, n: int = 1, now: float | None = None) -> RiskDecision:
        self.clock.record(n, now)
        ok, reason = self.clock.check(now)
        if not ok:
            return self._kill(reason)
        return RiskDecision(True, "fill_recorded")

    def on_disconnect(self, stale_sec: float) -> RiskDecision:
        if stale_sec >= DISCONNECT_PULL_SEC:
            return self._kill(
                f"disconnect {stale_sec:.1f}s >= {DISCONNECT_PULL_SEC:.0f}s — cancel all")
        return RiskDecision(True, "disconnect_within_grace")

    def on_exit(self) -> RiskDecision:
        return self._kill("process_exit — cancel all")

    def note_daily_pnl(self, daily_pnl_usd: Decimal) -> RiskDecision:
        """``daily_pnl_usd`` must be realized plus mark-to-market."""
        if daily_pnl_usd <= -self.limits.daily_loss_usd:
            return self._kill(
                f"daily_loss {daily_pnl_usd} <= -{self.limits.daily_loss_usd}")
        return RiskDecision(True, "pnl_ok")

    def underlying_usd(self, family: str) -> Decimal:
        total = ZERO
        for market, usd in self.market_usd.items():
            if family_for_series(series_of(market)) == family:
                total += Decimal(str(usd))
        return total

    def series_usd(self, series: str) -> Decimal:
        total = ZERO
        want = series.upper()
        for market, usd in self.market_usd.items():
            if series_of(market) == want:
                total += Decimal(str(usd))
        return total

    def check_quote(self, *, market: str, venue: str, add_usd: Decimal,
                    daily_pnl_usd: Decimal = ZERO,
                    now: float | None = None) -> RiskDecision:
        if self.killed:
            return RiskDecision(False, self.kill_reason, cancel_all=True)
        pnl = self.note_daily_pnl(daily_pnl_usd)
        if not pnl.allowed:
            return pnl
        ok, reason = self.clock.check(now)
        if not ok:
            return self._kill(reason)
        if add_usd < 0:
            return RiskDecision(False, "negative_notional")
        series = series_of(market)
        family = family_for_series(series)
        market_next = Decimal(str(self.market_usd.get(market, 0))) + add_usd
        if market_next > self.limits.per_market_usd:
            return RiskDecision(False, f"per_market {market} {market_next} > {self.limits.per_market_usd}")
        series_next = self.series_usd(series) + add_usd
        if series_next > self.limits.per_series_usd:
            return RiskDecision(False, f"per_series {series} {series_next} > {self.limits.per_series_usd}")
        # Event markets are not one factor. Only families that share an
        # underlying print (brent dailies and weeklies, BTC ladders, a
        # weather regime) share this cap. A Trump-vs-a-different-event
        # cluster is not inferred from the ticker.
        if family in ("commodity", "crypto", "weather"):
            under_next = self.underlying_usd(family) + add_usd
            if under_next > self.limits.per_underlying_usd:
                return RiskDecision(
                    False,
                    f"per_underlying {family} {under_next} > {self.limits.per_underlying_usd}")
        venue_next = Decimal(str(self.venue_usd.get(venue, 0))) + add_usd
        if venue_next > self.limits.per_venue_usd:
            return RiskDecision(False, f"per_venue {venue} {venue_next} > {self.limits.per_venue_usd}")
        gross = sum((Decimal(str(v)) for v in self.market_usd.values()), ZERO) + add_usd
        if gross > self.limits.gross_usd:
            return RiskDecision(False, f"gross {gross} > {self.limits.gross_usd}")
        return RiskDecision(True, "approved")

    def commit(self, market: str, venue: str, add_usd: Decimal) -> None:
        self.market_usd[market] = Decimal(str(self.market_usd.get(market, 0))) + add_usd
        self.venue_usd[venue] = Decimal(str(self.venue_usd.get(venue, 0))) + add_usd


def order_group_contracts_limit(per_market_cap_contracts: int) -> int:
    """Kalshi order-group contract limit, clamped to the documented 1..1_000_000."""
    return max(1, min(1_000_000, int(per_market_cap_contracts)))
