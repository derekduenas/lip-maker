"""Sovereign — All tunable parameters. DO NOT EXCEED risk limits."""

# --- Edge thresholds ---
MIN_EDGE_SINGLE = 0.15    # minimum edge — backtest shows 8pp loses, 15pp+ wins at 79%
MIN_EDGE_PARLAY = 0.06    # minimum edge on parlay trade

# --- Position sizing ---
KELLY_FRACTION = 0.25     # quarter-Kelly (2026-04-19): walkforward ROI not yet positive
MAX_TRADE_PCT = 0.08      # max % of bankroll per single trade
MAX_DAILY_DEPLOY = 0.40   # max % of bankroll deployed in one day
MAX_DAILY_TRADES = 5      # max trades per morning scan (take only the best)
DAILY_LOSS_CAP_PCT = 0.10  # halt day if realized PnL <= -10% bankroll (circuit breaker, added 2026-04-19)
MAX_TRADES_PER_EVENT = 3  # max trades on a single event (don't over-concentrate)
MIN_MARKET_PRICE = 0.05   # skip markets below 5c (already settled or no liquidity)
MAX_MARKET_PRICE = 0.95   # skip markets above 95c (already settled or no edge)

# --- Liquidity (Upgrade 7) ---
MIN_MARKET_VOLUME = 500   # $500 minimum volume to trade
MAX_BID_ASK_SPREAD = 0.05 # skip if spread > 5c

# --- High conviction (Upgrade 4) ---
HIGH_CONVICTION_MAX_PCT = 0.25  # 25% max for high-conviction trades (vs 15% normal)

# --- Fallback (Upgrade 10) ---
MIN_EDGE_FALLBACK = 0.12  # 12pp minimum for no-corpus fallback trades

# --- Junk Bond Strategy (Catboy playbook) ---
JUNK_BOND_ENABLED = True
JUNK_BOND_MIN_PROB = 0.95         # our estimated true probability must be 95%+
JUNK_BOND_MAX_PRICE = 0.90        # don't buy above 90c
JUNK_BOND_MIN_PRICE = 0.70        # don't buy below 70c (too much risk)
JUNK_BOND_MIN_EDGE = 0.05         # 5pp minimum (lower threshold — near-certain)
JUNK_BOND_MIN_CORPUS_N = 10       # deep corpus required for 95%+ confidence
JUNK_BOND_KELLY_MULTIPLIER = 1.5  # size LARGER — low variance

# --- High Variance Trades ---
HV_TRADE_ENABLED = True
HV_TRADE_MIN_PRICE = 0.10
HV_TRADE_MAX_PRICE = 0.60
HV_TRADE_MIN_EDGE = 0.15          # 15pp minimum
HV_TRADE_KELLY_MULTIPLIER = 0.5   # size SMALLER — high variance

# --- Compounding ---
COMPOUND_MODE = "moderate"  # conservative | moderate | aggressive

# --- Auto-ingest ---
MIN_CORPUS_QUALITY_SCORE = 0.45
AUTO_INGEST_QUARTERS_BACK = 8

# --- Data quality ---
MIN_BASE_RATE_N = 6       # minimum past events — backtest shows n<6 loses money

# --- Execution ---
MAX_PRICE_CHASE = 0.02    # never pay more than 2c above limit price
ORDER_TYPE = "LIMIT"      # NEVER market orders
PAPER_MODE = True         # flip to False only after 20+ paper trades validated
SHADOW_MODE = True        # records decisions + tracks outcomes, no orders placed
MIN_CONTRACTS = 10        # below this, fees eat the edge

# --- Kalshi API ---
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# --- FOMC term list (maps to active Kalshi mention markets) ---
FOMC_TERMS = [
    "inflation", "disinflation", "recession", "stagflation", "soft landing",
    "labor market", "unemployment", "rate cut", "rate hike", "pause",
    "neutral rate", "balance sheet", "quantitative tightening", "QT",
    "tariffs", "trade", "uncertainty", "confident", "gradual", "data dependent",
    "housing", "credit", "financial conditions", "headwinds", "resilient",
    "transitory", "pivot", "restrictive", "accommodation",
]

# --- Database ---
DB_PATH = "data/sovereign.db"
PAPER_DB_PATH = "data/paper_trades.db"
