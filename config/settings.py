"""LIP Market-Maker configuration.

Kalshi LIP program specs (source: CFTC filing Aug 2025 + Feb 2026 amendment):
  - Snapshot-based scoring, 1 per second, random within second
  - TargetSize: 100–20,000 contracts per market (set per-market by Kalshi)
  - DiscountFactor: ≤ 1.00 (per-market)
  - TimePeriodReward: $10–$1,000/day × days in period
  - Two-sided requirement (Feb 28, 2026): if either side fails TargetSize,
    the snapshot is excluded entirely
  - Payout at end of Time Period (up to 31 days), floor to $0.01, min $1.00
  - No enrollment — automatic for eligible members

Conservative defaults. Tune via experimentation.
"""
from __future__ import annotations
import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
DB_PATH      = str(DATA_DIR / "lip_maker.db")
LOG_PATH     = str(LOGS_DIR / "lip_maker.log")

# ── Kalshi API ────────────────────────────────────────────────────────────
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS_URL   = "wss://api.elections.kalshi.com/trade-api/ws/v2"

KALSHI_KEY_ID      = os.getenv("KALSHI_KEY_ID", "") or os.getenv("KALSHI_API_KEY", "")
KALSHI_KEY_PATH    = os.getenv("KALSHI_PRIVATE_KEY_PATH", str(PROJECT_ROOT / "config" / "kalshi_private_key.pem"))
KALSHI_RATE_LIMIT_SEC = 0.1  # REST calls are rate-limited; WS isn't

# ── Modes ─────────────────────────────────────────────────────────────────
# PAPER_MODE: log intended quotes but never hit order endpoints.
# SHADOW_MODE: also tracks LIP scoring against live book (data collection).
# When both False → live quoting (real money).
PAPER_MODE  = os.getenv("LIP_PAPER", "true").lower() == "true"
SHADOW_MODE = os.getenv("LIP_SHADOW", "true").lower() == "true"

# ── Bankroll / risk ───────────────────────────────────────────────────────
# BANKROLL_USD drives all risk caps. Read from LIP_BANKROLL env if set,
# else fall back to a floor of $80 (current starting point). After a
# deposit, set env: LIP_BANKROLL=5000 in lip-maker.service.
BANKROLL_USD = float(os.getenv("LIP_BANKROLL", "80"))

# Ramp-up phase: caps start SMALL and expand as daily PnL is positive.
# Day 0 deploy: 10% of bankroll gross. Day 7+ clean: 40%.
# Controlled by `RAMP_PHASE` env (1..4) → 10% / 20% / 30% / 40%.
RAMP_PHASE = int(os.getenv("LIP_RAMP_PHASE", "4"))  # paper=full
_ramp_fraction = {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.40}[max(1, min(4, RAMP_PHASE))]

MAX_BANKROLL_SHARE_PCT = _ramp_fraction
_total_gross_budget = BANKROLL_USD * MAX_BANKROLL_SHARE_PCT

# Paper mode overrides stay generous to exercise all markets.
# Live mode: derived from bankroll + ramp.
if PAPER_MODE:
    MAX_GROSS_PER_MARKET_USD  = 100.0
    MAX_GROSS_PER_SERIES_USD  = 500.0
    MAX_NET_INVENTORY_USD     = 50.0
    MAX_TOTAL_GROSS_USD       = 2000.0
    MAX_TOTAL_NET_USD         = 500.0
else:
    # Per-market: 5% of the total gross budget (spread across ~20 focused markets)
    MAX_GROSS_PER_MARKET_USD  = max(15.0, _total_gross_budget * 0.05)
    # 2026-04-22 (Skeptic audit): Per-series cap caps single-underlying flash
    # crash damage. Brent moves through 5+ strikes simultaneously — without
    # this cap, we could hit MAX_GROSS_PER_MARKET × 5+ on one event. 30% of
    # total budget = max 4-6 markets full-size on one underlying.
    MAX_GROSS_PER_SERIES_USD  = max(60.0, _total_gross_budget * 0.30)
    MAX_NET_INVENTORY_USD     = MAX_GROSS_PER_MARKET_USD * 0.5
    MAX_TOTAL_GROSS_USD       = _total_gross_budget
    MAX_TOTAL_NET_USD         = _total_gross_budget * 0.25

# ── HARD circuit breakers (live mode only) ────────────────────────────
# Daily loss cap: if realized P&L drops below this, halt all new quotes.
MAX_DAILY_LOSS_USD        = BANKROLL_USD * 0.05  # 5% of bankroll per day
# Session loss cap: if lifetime session loss exceeds this, halt.
MAX_SESSION_LOSS_USD      = BANKROLL_USD * 0.10  # 10% of bankroll total
# Single-fill cap: if one fill alone loses > this, halt and alert.
MAX_SINGLE_LOSS_USD       = BANKROLL_USD * 0.02  # 2% of bankroll per trade

# Minimum quote size per side (below this, LIP likely won't qualify us).
MIN_QUOTE_SIZE_CONTRACTS  = 10
# Target size per side (tuned per market; this is the default).
DEFAULT_QUOTE_SIZE_CONTRACTS = 25

# Reprice threshold: when best bid/ask moves by this many ticks, reprice.
REPRICE_TICK_THRESHOLD    = 1

# Cancel on stale data: if WS hasn't updated for this many seconds, pull quotes.
STALE_DATA_PULL_SECONDS   = 10

# ── LIP program filters ───────────────────────────────────────────────────
# Ignore markets where TimePeriodReward is too small to bother with.
# Tuned from initial discovery (1,077 programs): $10/day is the natural
# breakpoint — below that, WS/compute overhead isn't worth it.
MIN_REWARD_PER_DAY_USD    = 10.0
# Ignore markets with TargetSize we can't reasonably meet (we're small).
# 2026-04-22: raised 10000→19999 per Kalshi LIP spec ("Target Size will be
# greater than 100 contracts and less than 20,000 contracts"). Markets in
# (10000, 19999) range are likely SIG-tier we can't compete on, but
# discovery visibility is still valuable.
MAX_TARGET_SIZE_CONTRACTS = 19999
# DiscountFactor ≤ 1.00; 0.50 is modal (236 markets). Accepting 0.50
# forces us to quote at best-bid to score — which matches our strategy.
# Below 0.50 means the market is asking for depth-layering which we can't
# provide at our capital.
MIN_DISCOUNT_FACTOR       = 0.50

# ── Blocklist — markets where SIG/designated MMs dominate ────────────────
# Revisit after paper week; start with NFL/NBA/election flagships excluded.
SERIES_BLOCKLIST = {
    # Sports flagships (SIG has dedicated desk)
    "KXNFL", "KXNBA", "KXCFB", "KXMLB", "KXUFC",
    # Presidential/major political flagships
    "KXPRES", "KXPREZ", "KXELEC",
    # Fed rate decision flagships ($120M+ contracts, prop-desk dominated)
    "KXFEDDECISION",
}

# ── Quote pricing ─────────────────────────────────────────────────────────
# Where to place our quote relative to best bid/ask:
#   "join"    = match best bid/ask exactly
#   "inside"  = one tick tighter (0.01 inside)
#   "behind"  = one tick worse (0.01 outside)
# "join" is the default for LIP — we want to be AT best for scoring.
QUOTE_PLACEMENT = "join"

# Maximum bid-ask spread we'll quote — wider books are OK for LIP scoring
# (score = DiscountFactor^(ref - price) × Size; spread itself doesn't enter),
# but very wide books often indicate thin/adverse flow. 2026-04-22: raised
# 10→25 after agent audit found 27k rejections at spread 14-22c on otherwise-
# legit markets. Toxicity filter V2 provides the adverse-selection backstop.
MAX_SPREAD_CENTS = 25

# Minimum volume_24h to consider enrolling in a market.
MIN_MARKET_VOLUME_24H = 100

# ── Sizing relative to LIP TargetSize ─────────────────────────────────────
# Our quote size as a fraction of the market's TargetSize. Hitting ~0.25
# means we're contributing meaningfully to the size-at-best without
# dominating (and hurting our normalized score via self-dilution).
QUOTE_SIZE_AS_FRACTION_OF_TARGET = 0.20
