"""config/constitution.py — innait risk constitution.

Plain Python constants (no LLM). What actually enforces them (2026-10-01):

  - risk/sentinel.py checks them for the LEGACY execution.quote_manager
    path only (run_paper.py and tools). It does NOT wrap every order: the
    APEX engine (mm.unattended) does not call Sentinel; its risk layer is
    mm/risk.py, which reads several of these constants (fill-rate halt,
    exposure percentages, daily-loss caps) alongside its own LIP_* limits.
  - Sentinel skips all checks when PAPER_BYPASS is true and
    settings.PAPER_MODE is true (see below).

"No runtime override" holds only in the narrow sense that there is no
override flag: these are ordinary module attributes, so anything in the
process can reassign them (the tests do). Changing a limit for a
deployment still means editing this file and redeploying.

Limits live in this file because Python imports give us:
  - Type-checked constants
  - Version-controlled changes (git blame on the limits)
  - Single source of truth (other modules read from here)
  - Hot-reload-resistant (process restart required for limit changes)
"""
from __future__ import annotations

# ── Daily P&L caps (per ramp tier) ──────────────────────────────────────────
# Sentinel halts the entire engine when day's realized P&L breaches this.
# Different caps per ramp phase; the engine reads the right one based on
# the LIP_RAMP_PHASE env var.
#
# Ramp phase = 1: $50 bankroll (initial live) → $5 cap
# Ramp phase = 2: $500                        → $25 cap
# Ramp phase = 3: $2,000                      → $100 cap
# Ramp phase = 4: $5,000 (paper or full live) → $250 cap
MAX_DAILY_LOSS_BY_RAMP = {
    1: 5.0,
    2: 25.0,
    3: 100.0,
    4: 250.0,
}

# Once realized daily loss is this fraction of the cap, halt all NEW
# quoting (existing positions can settle). Tighter than 100% to leave
# headroom for inventory still in flight.
DAILY_LOSS_HALT_THRESHOLD = 0.95

# ── Concentration limits (as fraction of bankroll) ──────────────────────────
# Sentinel rejects an order if accepting it would breach these.
MAX_GROSS_EXPOSURE_PCT = 0.40        # total open gross / bankroll
MAX_PER_MARKET_PCT = 0.10            # single market / bankroll
MAX_PER_SERIES_PCT = 0.20            # single series / bankroll
MAX_PER_HEDGE_VENUE_PCT = 0.30       # per Kraken / PM / IBKR / etc.

# ── Order quality gates ─────────────────────────────────────────────────────
MIN_TWO_SIDED_BID_CENTS = 5          # both sides must have ≥5c bid
MIN_EXIT_LIQUIDITY_CONTRACTS = 50    # opposite side must have ≥50ct rest
MIN_QUOTE_SIZE_CONTRACTS = 10        # never quote smaller than this

# ── Rate limits (defense against bug-induced order storms) ──────────────────
MAX_QUOTES_PER_MINUTE = 120          # globally
MAX_QUOTES_PER_MARKET_PER_MINUTE = 20
MAX_FILLS_PER_MINUTE = 30            # if more, something is wrong; halt

# ── Hedge-aware gates ───────────────────────────────────────────────────────
# Series previously blocked for adverse selection are allowed IF a verified
# hedge counterpart exists at the venue. Enforced when sentinel sees a
# proposed quote on a blocklisted series.
REQUIRE_HEDGE_FOR_UNBLOCKLIST = True

# ── Behavioral gates ────────────────────────────────────────────────────────
# Sentinel skips ALL of its checks when this is True and settings.PAPER_MODE
# is True (paper: no real money at risk). Before 2026-10-01 Sentinel read a
# nonexistent settings.LIP_PAPER, so the bypass never actually fired.
PAPER_BYPASS = True

# ── Override discipline ─────────────────────────────────────────────────────
# Strategic plan: 24-hour cooling-off + written justification to relax any
# limit. Implemented operationally — Sentinel itself has no override flag.
# Limit changes go through git commit → review → deploy.
ALLOW_RUNTIME_OVERRIDE = False    # informational; nothing reads it (PAPER_BYPASS above is a bypass)
