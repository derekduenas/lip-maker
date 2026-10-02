"""Dislocation harvester configuration.

Tunings here gate every scanner. Conservative defaults: paper mode by default,
no live execution until operator flips DISLOCATION_LIVE=true and the
candidate has cleared the manual-review gate.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
DB_PATH      = str(DATA_DIR / "dislocation.db")
LOG_PATH     = str(LOGS_DIR / "dislocation.log")

# ── Modes ────────────────────────────────────────────────────────────────
# PAPER_MODE: log candidate trades, never execute.
# REVIEW_MODE: write candidates to review_queue table, await manual approve.
# LIVE_MODE: auto-execute (only after 30+ settlements pass model validation).
PAPER_MODE  = os.getenv("DISLOCATION_PAPER", "true").lower() == "true"
REVIEW_MODE = os.getenv("DISLOCATION_REVIEW", "true").lower() == "true"
LIVE_MODE   = (not PAPER_MODE) and os.getenv("DISLOCATION_LIVE", "false").lower() == "true"

# ── Capital ──────────────────────────────────────────────────────────────
# Bankroll allocated to dislocation prong (separate from LIP prong).
BANKROLL_USD = float(os.getenv("DISLOCATION_BANKROLL", "1000"))

# Hard cap per single convergence trade (% of bankroll).
MAX_TRADE_PCT_OF_BANKROLL = 0.10

# Hard cap on total deployed capital (% of bankroll).
MAX_DEPLOYED_PCT = 0.50

# ── Edge thresholds ──────────────────────────────────────────────────────
# Minimum cost-adjusted spread (in probability points) to flag a candidate.
# 3pp = e.g. Kalshi at 60% vs FF futures implied 65% after fees + slippage.
# Below this, costs eat the spread.
MIN_EDGE_PP = float(os.getenv("DISLOCATION_MIN_EDGE_PP", "3.0"))

# Minimum days to settlement (avoid same-day chop / settlement-tape risk).
MIN_DAYS_TO_SETTLE = 1

# Maximum days to settlement (capital efficiency — don't tie up for months).
MAX_DAYS_TO_SETTLE = 180

# ── Cost model ───────────────────────────────────────────────────────────
# Per-side trading costs in cents (round-trip = 2x). Conservative.
KALSHI_FEE_PCT       = 0.07    # 7% of value per fill (Kalshi default).
KALSHI_TICK_SLIPPAGE = 0.01    # 1 tick crossing the spread (worst case).
CME_FUTURES_FEE_USD  = 1.20    # per contract per side (round-trip $2.40).
PM_FEE_PCT           = 0.02    # 2% maker fee Polymarket (taker higher).
OPTIONS_FEE_USD      = 0.65    # per contract per side.
EQUITY_FEE_BPS       = 1.0     # 1bp slippage equities (assume zero comm).

# Funding cost on capital tied up (annualized, used for hold-cost math).
ANNUAL_FUNDING_PCT = 0.05

# ── Sizing (Kelly) ───────────────────────────────────────────────────────
# Fractional Kelly: 0.25 = quarter-Kelly. Full Kelly is too volatile in
# practice; quarter-Kelly preserves geometric growth with much lower drawdown.
KELLY_FRACTION = 0.25

# Hard floor / ceiling on per-trade size (USD).
MIN_TRADE_USD = 25.0
MAX_TRADE_USD = 500.0

# Adverse-excursion buffer: assume 2x the model-predicted MAE before settlement.
# Sizes positions smaller so margin call doesn't hit on convergence trades.
MAE_BUFFER_MULT = 2.0

# ── Scanner cadence ──────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = int(os.getenv("DISLOCATION_SCAN_SEC", "300"))   # 5 min
TOP_N_CANDIDATES  = int(os.getenv("DISLOCATION_TOP_N", "10"))

# ── Validation gate ──────────────────────────────────────────────────────
# Before any scanner can transition from REVIEW_MODE → LIVE_MODE, this many
# settled trades must show realized convergence within the predicted band.
MIN_VALIDATION_SETTLEMENTS = 30
VALIDATION_BAND_PP         = 5.0   # realized must be within ±5pp of model
