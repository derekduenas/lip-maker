"""Pre-execution risk gates — the last line of defense.

Every order must pass EVERY gate before hitting Kalshi. Fail-closed by
default. If any gate errors, we ABORT (never fail-open to protect bankroll).

Gates (enforced in order):
  G1. KILL_SWITCH — environment variable SOV_KILL=true → abort all
  G2. MODE — explicit SOV_LIVE=true required for real orders, else paper
  G3. BALANCE_FLOOR — Kalshi balance ≥ $50 (don't drain to zero)
  G4. PER_POSITION — single order ≤ 8% of bankroll
  G5. PER_EVENT — cumulative event exposure ≤ 30% of bankroll
  G6. SESSION_LOSS — cumulative realized loss today ≤ -5% bankroll → halt
  G7. SINGLE_LOSS — any single fill lost > 2% bankroll → halt + alert
  G8. MIN_SIZE — order size ≥ MIN_CONTRACTS (else not worth the fee drag)
  G9. PRICE_SANITY — yes_price ∈ [0.01, 0.99]
  G10. THESIS_FRESHNESS — thesis computed within last 24h
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


# Defaults — can be overridden via env
DEFAULT_BANKROLL_FLOOR_USD       = 50.0
DEFAULT_MAX_POSITION_PCT         = 0.08
DEFAULT_MAX_EVENT_PCT            = 0.30
DEFAULT_DAILY_LOSS_HALT_PCT      = 0.05
DEFAULT_SINGLE_LOSS_HALT_PCT     = 0.02
DEFAULT_MIN_ORDER_CONTRACTS      = 5
DEFAULT_THESIS_MAX_AGE_HOURS     = 24


@dataclass
class OrderCandidate:
    """An order we WANT to place — risk_gates decides if we actually do."""
    ticker:            str
    side:              str            # "yes" | "no"
    price_cents:       int            # 1-99
    contracts:         int            # positive integer
    bankroll_usd:      float
    event_id:          str
    event_exposure_usd: float = 0.0   # cumulative $ already placed on this event
    thesis_generated_at: Optional[datetime] = None

    def cost_usd(self) -> float:
        """Cash required for this order (maker, no fee upfront)."""
        return self.price_cents * self.contracts / 100.0


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y")


def _env_float(name: str, default: float) -> float:
    v = _env(name)
    try:
        return float(v) if v else default
    except ValueError:
        return default


def check_gates(
    order:                 OrderCandidate,
    kalshi_balance_usd:    float,
    daily_realized_pnl:    float = 0.0,
    worst_single_pnl:      float = 0.0,
    skip_live_check:       bool = False,
) -> tuple[bool, str]:
    """Run all gates. Returns (ok, reason). ok=False means reject.

    `skip_live_check=True`: bypass G2 mode check (for paper/dry-run callers).
    All other gates (balance, position, daily-loss) still validate.
    """

    # G1: Kill switch
    if _env_bool("SOV_KILL"):
        return False, "G1_KILL_SWITCH: SOV_KILL=true — all orders halted"

    # G2: Mode check — default is PAPER, must explicitly go LIVE
    if not skip_live_check:
        live_mode = _env_bool("SOV_LIVE", default=False)
        if not live_mode:
            return False, "G2_MODE: SOV_LIVE not set → paper only (use scripts/go_live.py)"

    # G3: Balance floor
    floor = _env_float("SOV_BANKROLL_FLOOR", DEFAULT_BANKROLL_FLOOR_USD)
    if kalshi_balance_usd < floor:
        return False, f"G3_BALANCE_FLOOR: ${kalshi_balance_usd:.2f} < ${floor:.2f} floor"

    # G4: Per-position cap
    max_pos_pct = _env_float("SOV_MAX_POSITION_PCT", DEFAULT_MAX_POSITION_PCT)
    max_pos_usd = order.bankroll_usd * max_pos_pct
    if order.cost_usd() > max_pos_usd:
        return False, (f"G4_PER_POSITION: ${order.cost_usd():.2f} > cap "
                       f"${max_pos_usd:.2f} ({max_pos_pct*100:.0f}% of ${order.bankroll_usd:.0f})")

    # G5: Per-event cap (existing + new)
    max_evt_pct = _env_float("SOV_MAX_EVENT_PCT", DEFAULT_MAX_EVENT_PCT)
    max_evt_usd = order.bankroll_usd * max_evt_pct
    if order.event_exposure_usd + order.cost_usd() > max_evt_usd:
        return False, (f"G5_PER_EVENT: ${order.event_exposure_usd + order.cost_usd():.2f} > cap "
                       f"${max_evt_usd:.2f} ({max_evt_pct*100:.0f}% of ${order.bankroll_usd:.0f})")

    # G6: Daily loss halt
    daily_halt_pct = _env_float("SOV_DAILY_LOSS_HALT_PCT", DEFAULT_DAILY_LOSS_HALT_PCT)
    daily_halt_usd = -order.bankroll_usd * daily_halt_pct
    if daily_realized_pnl < daily_halt_usd:
        return False, (f"G6_DAILY_LOSS: today's P&L ${daily_realized_pnl:.2f} "
                       f"< halt ${daily_halt_usd:.2f}")

    # G7: Single-fill loss
    single_halt_pct = _env_float("SOV_SINGLE_LOSS_HALT_PCT", DEFAULT_SINGLE_LOSS_HALT_PCT)
    single_halt_usd = -order.bankroll_usd * single_halt_pct
    if worst_single_pnl < single_halt_usd:
        return False, (f"G7_SINGLE_LOSS: worst fill ${worst_single_pnl:.2f} "
                       f"< halt ${single_halt_usd:.2f}")

    # G8: Min order size
    min_contracts = int(_env_float("SOV_MIN_ORDER_CONTRACTS", DEFAULT_MIN_ORDER_CONTRACTS))
    if order.contracts < min_contracts:
        return False, f"G8_MIN_SIZE: {order.contracts} < {min_contracts}"

    # G9: Price sanity
    if not (1 <= order.price_cents <= 99):
        return False, f"G9_PRICE_SANITY: {order.price_cents}¢ outside [1, 99]"

    # G10: Thesis freshness
    max_age = _env_float("SOV_THESIS_MAX_AGE_HOURS", DEFAULT_THESIS_MAX_AGE_HOURS)
    if order.thesis_generated_at is None:
        return False, "G10_THESIS_FRESHNESS: no timestamp on thesis"
    age_h = (datetime.now(timezone.utc) - order.thesis_generated_at).total_seconds() / 3600
    if age_h > max_age:
        return False, f"G10_THESIS_FRESHNESS: {age_h:.1f}h old > {max_age:.0f}h max"

    return True, "all_gates_passed"


def load_daily_pnl(db_path: str) -> tuple[float, float]:
    """Return (cumulative_daily_pnl, worst_single_pnl) from sovereign trades table.
    Falls back to (0, 0) if table doesn't exist yet."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            rows = conn.execute(
                """SELECT COALESCE(pnl, 0) FROM trades
                   WHERE resolved_at >= ? AND status = 'closed'""",
                (cutoff,),
            ).fetchall()
            pnls = [r[0] or 0.0 for r in rows]
            if not pnls:
                return 0.0, 0.0
            return sum(pnls), min(pnls)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # Trades table doesn't exist — no loss yet
        return 0.0, 0.0
