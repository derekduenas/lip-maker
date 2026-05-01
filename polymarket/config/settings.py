"""Polymarket US Maker — configuration.

Pillar 2 venue (companion to lip-maker on Kalshi).

Polymarket US (CFTC DCM via QCEX, relaunched Dec 2 2025):
  - Per-minute snapshot scoring (10,080 samples per weekly epoch)
  - Spread function: S(v,s) = ((v-s)/v)^2 × b — quadratic, harsh on
    off-mid quotes
  - Two-sided REQUIRED at midpoint extremes (<0.10 or >0.90)
  - Daily USDC payout at 00:00 UTC
  - 10 instruments per WS connection (need shards for top-N)
  - Ed25519 keypair auth, timestamp-signed
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
DB_PATH      = str(DATA_DIR / "polymarket_maker.db")
LOG_PATH     = str(LOGS_DIR / "polymarket_maker.log")

# ── Polymarket API ────────────────────────────────────────────────────────
# api.polymarket.us — for AUTH'd execution (Ed25519 keys required even for
# market data). Used when user has API keys configured.
PM_REST_BASE  = os.getenv("PM_REST_BASE", "https://api.polymarket.us")
PM_WS_PUBLIC  = os.getenv("PM_WS_PUBLIC", "wss://api.polymarket.us/v1/ws/markets")
PM_WS_PRIVATE = os.getenv("PM_WS_PRIVATE", "wss://api.polymarket.us/v1/ws/private")

# gamma-api.polymarket.com — OPEN public endpoint (no auth) for market
# discovery + rewards info + book snapshots. Returns clobRewards with
# rewardsDailyRate, rewardsMinSize, rewardsMaxSpread per market — exactly
# what the maker-rebate scorer needs. Use for public_recorder + scanner.
PM_GAMMA_BASE = os.getenv("PM_GAMMA_BASE", "https://gamma-api.polymarket.com")
PM_CLOB_BASE  = os.getenv("PM_CLOB_BASE",  "https://clob.polymarket.com")

# Auth: Ed25519 keypair generated at polymarket.us/developer
PM_API_KEY_ID    = os.getenv("PM_API_KEY_ID", "")
PM_API_PRIVATE_KEY_PATH = os.getenv(
    "PM_API_PRIVATE_KEY_PATH",
    str(PROJECT_ROOT / "config" / "polymarket_private_key.pem"),
)

# Rate limits per Polymarket docs (60 req/min shared)
PM_REST_RATE_PER_MIN = int(os.getenv("PM_REST_RATE_PER_MIN", "60"))
PM_WS_INSTRUMENTS_PER_CONN = 10  # hard cap per Polymarket docs

# ── Modes ─────────────────────────────────────────────────────────────────
PAPER_MODE  = os.getenv("PM_PAPER", "true").lower() == "true"
SHADOW_MODE = os.getenv("PM_SHADOW", "true").lower() == "true"

# ── Bankroll / risk ───────────────────────────────────────────────────────
# Polymarket bankroll separate from Kalshi (run on different DO box or env).
# Default $0 until user funds account; override via PM_BANKROLL env.
BANKROLL_USD = float(os.getenv("PM_BANKROLL", "0"))

# Phase ramp same pattern as Kalshi: 10/20/30/40% of bankroll.
PHASE = int(os.getenv("PM_PHASE", "1"))
_phase_fraction = {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.40}[max(1, min(4, PHASE))]
MAX_BANKROLL_SHARE_PCT = _phase_fraction
_total_gross_budget = BANKROLL_USD * MAX_BANKROLL_SHARE_PCT

if PAPER_MODE:
    # Paper-mode: generous to exercise system
    MAX_GROSS_PER_MARKET_USD  = 100.0
    MAX_GROSS_PER_SERIES_USD  = 500.0
    MAX_NET_INVENTORY_USD     = 50.0
    MAX_TOTAL_GROSS_USD       = 2000.0
else:
    MAX_GROSS_PER_MARKET_USD  = max(15.0, _total_gross_budget * 0.05)
    MAX_GROSS_PER_SERIES_USD  = max(60.0, _total_gross_budget * 0.30)
    MAX_NET_INVENTORY_USD     = MAX_GROSS_PER_MARKET_USD * 0.5
    MAX_TOTAL_GROSS_USD       = _total_gross_budget

# Circuit breakers (live mode)
MAX_DAILY_LOSS_USD   = BANKROLL_USD * 0.05
MAX_SESSION_LOSS_USD = BANKROLL_USD * 0.10
MAX_SINGLE_LOSS_USD  = BANKROLL_USD * 0.02

# ── Quoting parameters ────────────────────────────────────────────────────
# Polymarket scoring is QUADRATIC in spread distance — being 1c off mid
# costs us 4x what Kalshi did. Default to JOIN best (0c off mid).
QUOTE_PLACEMENT = "join"
MIN_QUOTE_SIZE_CONTRACTS  = 5      # Polymarket min order is typically smaller
DEFAULT_QUOTE_SIZE_CONTRACTS = 20

# Reprice when best moves by this many ticks
REPRICE_TICK_THRESHOLD    = 1

# Cancel on stale data: pull quotes if no WS update for this many seconds
STALE_DATA_PULL_SECONDS   = 10

# Max acceptable spread on a quoted market. Wider books = adverse selection
# trap (one fill at our quote, the other side moves and we eat MTM loss).
# Polymarket pricing is quadratic — at 5c spread, our score is already 25%
# of best. Past that, not worth it.
MAX_SPREAD_FRACTION = 0.30  # 2026-04-30 v3: allow real weather/political spreads (30c) but reject stub books (>30c = empty)  # 2026-04-30: 0.05→0.85 — PM weather/political books naturally have 50c+ spreads. Filter was killing all candidates.  # = 5 cents on a [0,1] price grid

# Two-sided requirement varies by midpoint position:
#   midpoint in [0.10, 0.90] → can score single-sided at 1/3 efficiency
#   midpoint outside → must be two-sided or score=0
# We default to ALWAYS two-sided to maximize score across all markets.
TWO_SIDED_REQUIRED = True

# ── LIP filters ───────────────────────────────────────────────────────────
# Min weekly market reward to consider (skip pennies)
MIN_REWARD_PER_DAY_USD    = 5.0  # lower than Kalshi since pools are smaller

# ── Series blocklist ──────────────────────────────────────────────────────
# Series to exclude. Sports flagships dominated by SIG/Susquehanna.
SERIES_BLOCKLIST: set[str] = set()

# ── Sizing relative to TargetSize / b parameter ───────────────────────────
QUOTE_SIZE_AS_FRACTION_OF_TARGET = 0.20

# Series-level multipliers (analogous to Kalshi #119 winners table)
# Empty until we have data.
SIZE_MULTIPLIER_BY_SERIES: dict[str, float] = {}
DEFAULT_SIZE_MULTIPLIER = 1.0
