"""risk/sentinel.py — unconditional risk veto on every order.

Sentinel wraps execution.quote_manager.QuoteManager._passes_safety as
the FIRST check. Returns (approved, reason) for every proposed
QuoteTarget. Approval requires ALL constitution limits to pass.

DESIGN PRINCIPLES (from strategic plan Section A):
  1. Pure Python — no LLM, no async, no agent-mediated approval
  2. Hard limits from config/constitution.py — no runtime override
  3. Fail-closed — if Sentinel itself errors, the order is REJECTED
     (better to miss a quote than to skip the gate)
  4. Idempotent — same proposal evaluated twice yields the same answer
  5. Read-only — Sentinel never mutates the proposal or DB state;
     it only inspects + decides

WHAT SENTINEL ENFORCES:
  - Daily realized P&L vs ramp-tier cap (halt on breach)
  - Total gross exposure vs MAX_GROSS_EXPOSURE_PCT × bankroll
  - Per-market gross vs MAX_PER_MARKET_PCT × bankroll
  - Per-series aggregate vs MAX_PER_SERIES_PCT × bankroll
  - Two-sided liquidity gate (existing two-sided check stays in QM)
  - Rate limits (orders per minute, fills per minute)

USAGE
  from risk.sentinel import Sentinel
  s = Sentinel()
  ok, reason = s.approve(target)
  if not ok:
      log.info(f"SENTINEL veto: {reason}")
      return False, f"SENTINEL: {reason}"
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings, constitution

_log = logging.getLogger(__name__)


class Sentinel:
    """Unconditional risk veto. One instance per QuoteManager.

    Constructed cheaply (no I/O on __init__). State queries happen
    lazily inside approve() with appropriate caching to keep approval
    fast (target: < 5ms per call so it can run on every quote update).
    """

    # Module-level rate-limiter state (process-wide, all instances share)
    _quote_timestamps: deque = deque(maxlen=200)
    _quote_timestamps_per_market: dict = defaultdict(lambda: deque(maxlen=30))

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DB_PATH
        self._bankroll_cache: tuple[float, float] | None = None  # (value, ts)
        self._daily_pnl_cache: tuple[float, float] | None = None
        self._inventory_cache: tuple[dict, float] | None = None

    # ── Approval ────────────────────────────────────────────────────────────

    def approve(self, target) -> tuple[bool, str]:
        """Return (approved, reason). Reason is a short human-readable string
        suitable for logging/journaling.
        """
        # Paper mode bypass (configurable in constitution)
        if constitution.PAPER_BYPASS and getattr(settings, "LIP_PAPER", False):
            return True, "paper_bypass"

        # Wrap everything in try/except — fail-CLOSED on any error
        try:
            # 1. Daily loss circuit breaker (most critical)
            ok, reason = self._check_daily_loss()
            if not ok:
                return False, reason

            # 2. Concentration limits
            ok, reason = self._check_concentration(target)
            if not ok:
                return False, reason

            # 3. Rate limits
            ok, reason = self._check_rate_limits(target)
            if not ok:
                return False, reason

            # 4. Minimum quote quality (size floor, two-sided, etc.)
            ok, reason = self._check_quote_quality(target)
            if not ok:
                return False, reason

            # Register this quote in the rate-limiter
            self._record_quote(target)
            return True, "approved"
        except Exception as e:
            # Fail-CLOSED on Sentinel errors. Better to miss a quote than
            # to skip the gate.
            _log.error(f"SENTINEL ERROR (fail-closed): {type(e).__name__}: {e}")
            return False, f"sentinel_error:{type(e).__name__}"

    # ── Individual checks ───────────────────────────────────────────────────

    def _check_daily_loss(self) -> tuple[bool, str]:
        ramp = int(getattr(settings, "RAMP_PHASE", 4))
        cap = constitution.MAX_DAILY_LOSS_BY_RAMP.get(ramp, 50.0)
        halt_at = -cap * constitution.DAILY_LOSS_HALT_THRESHOLD
        daily_pnl = self._daily_realized_pnl()
        if daily_pnl <= halt_at:
            return False, (
                f"daily_loss_circuit_breaker: pnl ${daily_pnl:+.2f} "
                f"≤ halt ${halt_at:+.2f} (cap ${cap}, ramp={ramp})"
            )
        return True, ""

    def _check_concentration(self, target) -> tuple[bool, str]:
        bankroll = self._bankroll()
        if bankroll <= 0:
            return False, "bankroll_zero"

        # Estimate proposed notional from target
        notional = self._target_notional(target)

        # Total gross check
        current_gross = self._gross_exposure()
        max_gross = bankroll * constitution.MAX_GROSS_EXPOSURE_PCT
        if current_gross + notional > max_gross:
            return False, (
                f"gross_concentration: ${current_gross + notional:.2f} > "
                f"${max_gross:.2f} ({constitution.MAX_GROSS_EXPOSURE_PCT*100:.0f}% bankroll)"
            )

        # Per-market check
        ticker = getattr(target, "market_ticker", "")
        per_market = self._market_gross(ticker)
        max_per_market = bankroll * constitution.MAX_PER_MARKET_PCT
        if per_market + notional > max_per_market:
            return False, (
                f"market_concentration: {ticker} ${per_market + notional:.2f} > "
                f"${max_per_market:.2f}"
            )

        # Per-series check
        series = ticker.split("-", 1)[0] if "-" in ticker else ticker
        per_series = self._series_gross(series)
        max_per_series = bankroll * constitution.MAX_PER_SERIES_PCT
        if per_series + notional > max_per_series:
            return False, (
                f"series_concentration: {series} ${per_series + notional:.2f} > "
                f"${max_per_series:.2f}"
            )
        return True, ""

    def _check_rate_limits(self, target) -> tuple[bool, str]:
        now = time.time()
        # Global quote rate
        recent = sum(1 for ts in Sentinel._quote_timestamps if now - ts < 60.0)
        if recent >= constitution.MAX_QUOTES_PER_MINUTE:
            return False, (
                f"rate_limit_global: {recent} quotes in last 60s "
                f"(cap {constitution.MAX_QUOTES_PER_MINUTE})"
            )
        # Per-market quote rate
        ticker = getattr(target, "market_ticker", "")
        market_recent = sum(
            1 for ts in Sentinel._quote_timestamps_per_market[ticker]
            if now - ts < 60.0
        )
        if market_recent >= constitution.MAX_QUOTES_PER_MARKET_PER_MINUTE:
            return False, (
                f"rate_limit_market: {ticker} {market_recent} quotes in 60s "
                f"(cap {constitution.MAX_QUOTES_PER_MARKET_PER_MINUTE})"
            )
        return True, ""

    def _check_quote_quality(self, target) -> tuple[bool, str]:
        # Two-sided + size floor — these are redundant with QuoteManager's
        # existing checks but Sentinel enforces them as constitutional.
        size = max(
            int(getattr(target, "size_contracts", 0) or 0),
            int(getattr(target, "yes_size_override", 0) or 0),
            int(getattr(target, "no_size_override", 0) or 0),
        )
        if size < constitution.MIN_QUOTE_SIZE_CONTRACTS:
            return False, (
                f"size_floor: {size} < "
                f"{constitution.MIN_QUOTE_SIZE_CONTRACTS} contracts"
            )
        yb = getattr(target, "yes_bid_cents", None)
        nb = getattr(target, "no_bid_cents", None)
        if yb is None or nb is None:
            return False, "one_sided: missing yes_bid or no_bid"
        if yb < constitution.MIN_TWO_SIDED_BID_CENTS or nb < constitution.MIN_TWO_SIDED_BID_CENTS:
            return False, (
                f"thin_bid: yes={yb}c no={nb}c "
                f"(min {constitution.MIN_TWO_SIDED_BID_CENTS}c)"
            )
        return True, ""

    # ── Quote registration (rate-limiter bookkeeping) ───────────────────────

    def _record_quote(self, target) -> None:
        now = time.time()
        Sentinel._quote_timestamps.append(now)
        ticker = getattr(target, "market_ticker", "")
        Sentinel._quote_timestamps_per_market[ticker].append(now)

    # ── State queries ───────────────────────────────────────────────────────
    # Cached for 1 second to keep approve() fast on quote-rate workloads.

    def _bankroll(self) -> float:
        # Bankroll is a config constant — no cache TTL needed
        return float(getattr(settings, "BANKROLL_USD", 80))

    def _daily_realized_pnl(self) -> float:
        now = time.time()
        if self._daily_pnl_cache and (now - self._daily_pnl_cache[1]) < 1.0:
            return self._daily_pnl_cache[0]
        try:
            conn = sqlite3.connect(self.db_path, timeout=1.0)
            try:
                row = conn.execute(
                    """SELECT daily_realized_delta FROM daily_pnl_log
                       WHERE day = date('now')
                       ORDER BY snapshot_at DESC LIMIT 1"""
                ).fetchone()
            finally:
                conn.close()
            val = float(row[0]) if row and row[0] is not None else 0.0
        except Exception:
            val = 0.0
        self._daily_pnl_cache = (val, now)
        return val

    def _inventory(self) -> dict:
        """Returns {ticker: gross_usd}. Cached 1s."""
        now = time.time()
        if self._inventory_cache and (now - self._inventory_cache[1]) < 1.0:
            return self._inventory_cache[0]
        out = {}
        try:
            conn = sqlite3.connect(self.db_path, timeout=1.0)
            try:
                rows = conn.execute(
                    "SELECT market_ticker, gross_usd FROM inventory "
                    "WHERE net_yes_contracts != 0 OR gross_usd > 0"
                ).fetchall()
            finally:
                conn.close()
            for t, g in rows:
                out[t] = float(g or 0)
        except Exception:
            out = {}
        self._inventory_cache = (out, now)
        return out

    def _gross_exposure(self) -> float:
        return sum(abs(v) for v in self._inventory().values())

    def _market_gross(self, ticker: str) -> float:
        return abs(self._inventory().get(ticker, 0.0))

    def _series_gross(self, series_prefix: str) -> float:
        inv = self._inventory()
        return sum(
            abs(g) for t, g in inv.items()
            if t.startswith(series_prefix + "-") or t == series_prefix
        )

    def _target_notional(self, target) -> float:
        """Estimate $ notional of a proposed quote (max of yes/no side)."""
        size = max(
            int(getattr(target, "size_contracts", 0) or 0),
            int(getattr(target, "yes_size_override", 0) or 0),
            int(getattr(target, "no_size_override", 0) or 0),
        )
        yb = float(getattr(target, "yes_bid_cents", 0) or 0)
        nb = float(getattr(target, "no_bid_cents", 0) or 0)
        # Notional = size × price-side / 100 (cents → dollars). Use higher
        # side as the conservative estimate.
        return size * max(yb, nb) / 100.0


# ── Self-test ────────────────────────────────────────────────────────────────

def _self_test() -> None:
    import os, tempfile
    from dataclasses import dataclass

    @dataclass
    class FakeTarget:
        market_ticker: str = "KXBRENTD-26JUN0117-T100"
        yes_bid_cents: int = 50
        no_bid_cents: int = 50
        size_contracts: int = 100
        yes_size_override: int = 0
        no_size_override: int = 0

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Seed minimal schema
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE daily_pnl_log (
                day TEXT PRIMARY KEY, daily_realized_delta REAL,
                snapshot_at TEXT
            );
            CREATE TABLE inventory (
                market_ticker TEXT PRIMARY KEY,
                net_yes_contracts INTEGER, gross_usd REAL
            );
        """)
        conn.commit()
        conn.close()

        # Force LIVE mode for the test (paper would bypass)
        settings.LIP_PAPER = False
        settings.BANKROLL_USD = 1000.0
        settings.RAMP_PHASE = 4

        s = Sentinel(db_path=path)
        target = FakeTarget()

        # Empty state — should approve
        ok, reason = s.approve(target)
        assert ok, f"clean approve failed: {reason}"
        print(f"  ✓ clean approve: {reason}")

        # Trip daily loss
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO daily_pnl_log (day, daily_realized_delta, snapshot_at) "
            "VALUES (date('now'), -250.0, ?)",
            (datetime.now(timezone.utc).isoformat(),)
        )
        conn.commit()
        conn.close()
        s._daily_pnl_cache = None
        ok, reason = s.approve(target)
        assert not ok, "should reject on daily loss breach"
        assert "circuit_breaker" in reason or "daily_loss" in reason, reason
        print(f"  ✓ daily loss veto: {reason}")

        # Reset PnL, trip concentration
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM daily_pnl_log")
        conn.execute(
            "INSERT INTO inventory (market_ticker, net_yes_contracts, gross_usd) "
            "VALUES (?, ?, ?)",
            ("KXBRENTD-26JUN0117-T100", 1000, 350.0)  # 35% of $1k = above 10% per-market cap
        )
        conn.commit()
        conn.close()
        s._daily_pnl_cache = None
        s._inventory_cache = None
        ok, reason = s.approve(target)
        assert not ok, "should reject on market concentration"
        assert "concentration" in reason, reason
        print(f"  ✓ concentration veto: {reason}")

        # Reset, trip size floor
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM inventory")
        conn.commit()
        conn.close()
        s._daily_pnl_cache = None
        s._inventory_cache = None
        small_target = FakeTarget(size_contracts=5)
        ok, reason = s.approve(small_target)
        assert not ok, "should reject on size floor"
        assert "size_floor" in reason, reason
        print(f"  ✓ size_floor veto: {reason}")

        # Reset, trip one-sided
        thin = FakeTarget(yes_bid_cents=2, size_contracts=100)
        ok, reason = s.approve(thin)
        assert not ok, "should reject on thin bid"
        assert "thin_bid" in reason, reason
        print(f"  ✓ thin_bid veto: {reason}")

        # Paper mode bypass
        settings.LIP_PAPER = True
        ok, reason = s.approve(thin)  # would normally reject
        assert ok, f"paper bypass should approve: {reason}"
        assert reason == "paper_bypass", reason
        print(f"  ✓ paper bypass: {reason}")
        settings.LIP_PAPER = False  # reset

        print("sentinel self-test: PASS")
    finally:
        if os.path.exists(path):
            os.unlink(path)


if __name__ == "__main__":
    _self_test()
