"""ARGUS global configuration.

Modes mirror dislocation/config.py pattern:
    PAPER_MODE  — log predictions only, never place orders
    REVIEW_MODE — also persist actionable candidates to review_queue
    LIVE_MODE   — execute trades (requires per-brain validation gate passed)

Brains opt in via the BRAIN_REGISTRY map. Brains whose
backtest test-Brier > BRIER_LIVE_GATE remain paper-only regardless of
LIVE_MODE flag.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR     = PROJECT_ROOT / "data" / "argus"
CACHE_DIR    = PROJECT_ROOT / "data" / "cache"
LOG_PATH     = PROJECT_ROOT / "logs" / "argus.log"

# ── Modes ─────────────────────────────────────────────────────────────────
PAPER_MODE   = os.getenv("ARGUS_PAPER",  "true").lower() == "true"
REVIEW_MODE  = os.getenv("ARGUS_REVIEW", "true").lower() == "true"
LIVE_MODE    = os.getenv("ARGUS_LIVE",   "false").lower() == "true"

# ── Bankroll ──────────────────────────────────────────────────────────────
# Fresh — does NOT inherit from LIP. Defaults small for paper.
BANKROLL_USD = float(os.getenv("ARGUS_BANKROLL", "1000"))

# ── Validation gates ──────────────────────────────────────────────────────
BRIER_LIVE_GATE = 0.20    # brain must hit Test Brier <= this to graduate
MIN_BACKTEST_N  = 20      # need >= this many test samples for the gate

# ── Edge thresholds (scanner) ─────────────────────────────────────────────
MIN_EDGE_PP    = 8.0       # raw |our_p - market_p| in percentage points
MIN_CONFIDENCE = 0.50      # brain self-confidence floor

# ── Sizing ────────────────────────────────────────────────────────────────
KELLY_FRACTION            = 0.25    # quarter-Kelly default
MAX_TRADE_PCT_OF_BANKROLL = 0.05    # 5% per trade
MAX_DEPLOYED_PCT          = 0.50    # 50% of bankroll across all open
MAX_TRADE_USD             = 200.0
MIN_TRADE_USD             = 5.0
MAE_BUFFER_MULT           = 2.0     # size assuming 2x mean adverse excursion

# ── Brain registry ────────────────────────────────────────────────────────
# Populated by individual brain modules at import time.
BRAIN_REGISTRY: dict = {}


def ensure_dirs() -> None:
    """Create cache + data dirs lazily — call from adapter init, not at import."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    print(f"PAPER_MODE  = {PAPER_MODE}")
    print(f"REVIEW_MODE = {REVIEW_MODE}")
    print(f"LIVE_MODE   = {LIVE_MODE}")
    print(f"BANKROLL    = ${BANKROLL_USD}")
    print(f"BRIER gate  = {BRIER_LIVE_GATE} (n>={MIN_BACKTEST_N})")
