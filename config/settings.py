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

# 2026-05-02 PREDATOR: split per-market cap from total-budget multiplier.
# Was: same _ramp_fraction served BOTH the per-market gate AND the total
# gross multiplier. At PHASE 4 (40%) per-market = $682 cap on $1.7k cash,
# meaning 8 markets need $5.5k = impossible with our bankroll. Result:
# top-N rank picks like KXF1DELAY ($500/d pool) got blocked by per-market
# cap and we missed them. New: per-market cap = 12% (≈$200 of $1.7k cash,
# fits 8-10 markets), total budget multiplier stays at ramp_fraction.
MAX_BANKROLL_SHARE_PCT = 0.50  # 2026-05-03 funnel-driven fix: code applies this as TOTAL (not per-market as comment claimed). Was 18% binding at $298 = blocked all but 3 markets. MAX_TOTAL_GROSS_USD is the real total cap; this ceiling now backstops it (50% × bankroll > total gross budget).
_total_gross_budget = BANKROLL_USD * _ramp_fraction

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
    MAX_GROSS_PER_MARKET_USD  = max(50.0, _total_gross_budget * 0.20)  # 2026-04-30: 5%→20%
    # 2026-04-22 (Skeptic audit): Per-series cap caps single-underlying flash
    # crash damage. Brent moves through 5+ strikes simultaneously — without
    # this cap, we could hit MAX_GROSS_PER_MARKET × 5+ on one event. 30% of
    # total budget = max 4-6 markets full-size on one underlying.
    MAX_GROSS_PER_SERIES_USD  = max(400.0, _total_gross_budget * 0.50)  # 2026-05-03 funnel-driven: was $160 binding on KXBTCMINMON/XRPMAXMON ($178 > cap). Strikes need ~$210 to qualify; new cap allows 2 strikes per series.
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
MIN_QUOTE_SIZE_CONTRACTS  = 25  # 2026-04-30 concentration: was 10, bumped for bigger share

# === Sprint 4 #1 — Kelly fractional sizing ===
KELLY_SIZING_ENABLED = True
KELLY_FRACTION = 0.25
KELLY_MIN_MULT = 0.5
KELLY_MAX_MULT = 1.5
KELLY_TARGET_HEADROOM = 1.5
# Target size per side (tuned per market; this is the default).
DEFAULT_QUOTE_SIZE_CONTRACTS = 75  # 2026-04-30 concentration: was 25, 3x bigger to materially move share

# Reprice threshold: when best bid/ask moves by this many ticks, reprice.
REPRICE_TICK_THRESHOLD    = 1

# Unreliable-futures price-conviction gate (#102, 2026-04-28):
# When fair-value confidence is "unreliable" (Cocoa/Sugar/HOIL) or "unknown",
# we have no way to verify which side will settle. The orderbook itself is
# the only signal: a bid ≥ this many cents on either side means strong
# directional consensus we can't independently verify, so we'd be quoting
# NO at 70c on a market that settles YES. Skip entirely. Set conservative
# (65c) to avoid false positives on legitimate near-coin-flip markets.
UNRELIABLE_FUTURES_MAX_BID = 65

# Active inventory skew (#97): when holding a net position, bias the
# offset-side quote LARGER and the same-side quote SMALLER, by up to this
# fraction of base size. 0.5 means a fully-sized base (25 contracts) can
# skew ±12 contracts (NO=37, YES=13 if we're long YES). Keeps top-of-book
# presence on both sides for LIP scoring while accelerating inventory
# unwind. Cap also bounded by abs(net_yes) so we don't over-skew on
# small inventory imbalances.
INVENTORY_SKEW_FRACTION = 0.5

# Tick backoff in volatile periods (#98): skip reprice cycles when best
# bid moves > VOLATILITY_BACKOFF_TICKS within VOLATILITY_WINDOW_SEC.
# In hot markets, our replacement quote is always the slow-second-mouse
# — informed flow moves first, hits our stale quote second. Backing off
# 1-2 cycles lets the market settle before we re-engage. 5c over 10s
# is the threshold (FOMC-style move = 8-15c in seconds, normal = 1-2c).
VOLATILITY_WINDOW_SEC     = 10
VOLATILITY_BACKOFF_TICKS  = 5

# Pre-settlement cancel window (#104, 2026-04-28): cancel all quotes on a
# market in the last X minutes before close. Friday W-series weekly
# commodities lose $60-90 in last 30 min as informed flow piles in for
# settlement. Last 30 min of rebate accrual is tiny ($1-2/market) vs
# typical adverse fill cost ($5-30/market). Per Apr 27 audit estimate:
# +$60/wk lift from this gate alone. 30 min default.
PRE_SETTLEMENT_CANCEL_MIN = 30

# Zombie cancel threshold (#114): heartbeat sweeps cancel any resting order
# whose price is ≥this many cents below current best on its side. Catches
# orders that drifted from best because reprice was blocked (safety gate,
# WS lag, stale book between updates). At gap=3¢, DF=0.5 score is
# 0.5^3 = 0.125 — already 87.5% degraded vs at-best. Worth freeing capital.
ZOMBIE_GAP_CENTS          = 3

# Cancel on stale data: if WS hasn't updated for this many seconds, pull quotes.
STALE_DATA_PULL_SECONDS   = 10

# ── LIP program filters ───────────────────────────────────────────────────
# Ignore markets where TimePeriodReward is too small to bother with.
# Tuned from initial discovery (1,077 programs): $10/day is the natural
# breakpoint — below that, WS/compute overhead isn't worth it.
MIN_REWARD_PER_DAY_USD    = 5.0  # 2026-05-03 ceiling-break: capital_allocator picks by yield-per-$, not raw pool — small pools with low comp still rank well. Lowering 15→5 unlocks ~600 more candidates the allocator can choose from. Total throughput ceiling lifts from $3.3k to ~$6-8k/mo.
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
MIN_DISCOUNT_FACTOR       = 0.20  # 2026-05-06: 0.50→0.20 unlocks KXMIDTERMMOV ($52k/d pool, 2082 markets) + KXMAKARY + KXLAMAYOR. Bleed monitor + capital allocator still gate per-market.

# ── Blocklist — markets where SIG/designated MMs dominate ────────────────
# Revisit after paper week; start with NFL/NBA/election flagships excluded.
SERIES_BLOCKLIST = {
    # Sports flagships (SIG has dedicated desk)
    # 2026-05-05 SURGICAL: family bans replaced with specific SIG-dominated subseries.
    # Removed KXNFL/KXNBA/KXCFB/KXMLB/KXUFC family bans — they excluded $5-7k/day
    # in legitimate low-comp prop pools (KXNBAMENTION, KXNBACOACH, KXMLBMANAGEROUT,
    # Anthony Edwards, Aaron Boone, IPL, EPL, etc.). Auto-prune still active as
    # safety net (-$5 over 3 settles) — anything that bleeds gets caught fast.
    "KXNBAMVP", "KXNBAFINALS", "KXNBACHAMP", "KXNBAROY", "KXNBASIXTHMAN",
    "KXMLBWS", "KXMLBMVP", "KXMLBCYY",
    "KXNFLSB", "KXNFLMVP", "KXNFLCONFCHAMP",
    "KXCFBNATL", "KXCFBHEISMAN",
    "KXUFCCHAMP",
    # Presidential/major political flagships
    "KXPRES", "KXPREZ", "KXELEC",
    # Fed rate decision flagships ($120M+ contracts, prop-desk dominated)
    "KXFEDDECISION",
    # 2026-04-27 PERMANENT BANS (audit-confirmed money-losers — Apr 21-26):
    # Net PnL = rebate − settlement losses. These series consistently bled
    # via adverse selection that exceeded any rebate captured.
    "KXCOFFEEW",   # net -$61.61 — biggest single loser, unreliable futures feed
    "KXSILVERD",   # net -$12.91 — $0 rebate captured, pure adverse selection
    "KXSILVERMON", # ditto, monthly variant
    # KXLITHIUMW: UNBLOCKED 2026-04-29 — backfill shows 7d NET +$6.36 (rebate $15.96 covers realized -$9.60)
    "KXLCATTLEW",  # high toxicity flags
    # 2026-04-28 BANS (unrealized -$21 audit, slow political binaries):
    "KXVOTEHUBTRUMPUPDOWN", # Trump approval, -$11.25 unreal directional drift
    "KXRECSSNBER", # Recession-by-2027 binary, -$9.90 unreal mean-reversion trap
    # Note: KXSUGARW (-$0.31), KXHOILW (+$11.63 net) excluded — borderline
    # cases. Re-evaluate after a clean week of post-blocklist data.
    # 2026-05-01 BANS (24h audit, govt-bill binaries with $0 rebate capture):
    "KXDHSFUND",      # net -$12.15 in single day, $0 rebate, directional bleed
    "KXDHSCOMPONENT", # net -$10.44, TSA/agency confirmation binaries
    # 2026-05-02 BANS (per-series auto-prune scan, 7d net < -$5):
    "KXWH",           # net -$17.40 over 8 settlements (whale-watch binary)
    "KXNICKELMON",    # net -$8.68 over 40 settlements (nickel monthly strikes — high-freq small bleed)
    "KXAAAGAS",       # net -$98 in single day 2026-05-04, biggest weekly loser (unreliable AAA gas feed)
    "KXHIGHLAX",      # net -$90 in 7d (LA HIGH temp — NWS-anchored adverse selection)
    "KXLOWTLAX",      # net -$45 in 7d (LA LOW temp — same NWS bleed)
    "KXHIGHLA",       # no-T variant (same family)
    "KXLOWLA",        # no-T variant
    "KXLOWTMIA",      # net -$18 in 12h post-LAX-block (Miami LOW temp — same NWS adverse-selection family)
    "KXLOWMIA",       # no-T variant
    "KXMETGALA",      # net -$65 in 6h 2026-05-05 (Met Gala — entertainment prop, sharp insider edge)
    "KXEOWEEK",       # net -$48 in 6h 2026-05-05 (executive orders weekly — political insider edge)
    # 2026-05-12 EVENT-BINARY BANS (bleed_monitor flagged 99% losses):
    # Long-dated one-shot binaries where two-sided LIP fills create
    # unbounded directional decay. Diagnosed 2026-05-12 from $135.89
    # single-batch ban event. Days-to-settle gate (EVENT_BINARY_MAX_DAYS)
    # backstops these; explicit blacklist prevents repeat enrolment if
    # series re-spawn with shorter resolution windows.
    "KXBLUEWAVECOMBO",       # 2026 midterms compound (House+Senate Dem) — 265d
    "KXGROK",                # xAI Grok release timing — 49d, decayed to 0.7%
    "KXJIMMYKIMMELFIRED",    # late-night host firing — 234d, decayed to 1%
    "KXJUDGECOUNT",          # federal judge confirmations — multi-tick monthly
    "KXKANYE",               # Kanye-* event family (releases, news)
    "KXKANYEISRAEL",         # Kanye-Israel statement timing
    "KXBALLONDOR",           # Ballon d'Or winner — once/year long-dated
    "KXFEDEND",              # "End of Fed" hypothetical — 4yr+ horizon
    # KXFEDDECISION already in list above (kept; flagship)
    # 2026-05-12 v2: geopolitical/event "weeklies" that previously slipped
    # through the suffix-only is_repeating_series fallback.
    "KXHORMUZ",              # Strait of Hormuz events — was -$15 on T20+T200
    "KXMAKARYOUT",           # FDA Commissioner firing — was -$7
    # 2026-05-13: profitability audit caught KXTRUEV as the BIGGEST ongoing
    # leak — 14d net -$229 across 155 fills (rebate +$30 vs realized -$258),
    # textbook adverse-selection-disguised-as-maker. Same pattern that ate
    # KXHORMUZ before the prefix-gate. series_auto_prune SHOULD have caught
    # this (7d net < -$5, n>=3) but its per-ticker bans expire 7d while new
    # KXTRUEV-* strikes respawn daily — bans never aggregate to the series.
    # FOLLOW-UP: investigate why auto_prune doesn't escalate to
    # SERIES_BLOCKLIST after N consecutive cycles flag the same prefix.
    "KXTRUEV",
}

# 2026-05-12: event-binary days-to-settle cap. Markets where settle is
# more than this many days out AND the series is not on the recurring
# whitelist (engine/lip_discovery.is_repeating_series) get rejected at
# discovery time. Backstops the SERIES_BLOCKLIST against newly-spawned
# event binaries we haven't manually banned.
EVENT_BINARY_MAX_DAYS = 60

# 2026-05-12 EXIT-LIQUIDITY GATE: depth_probe previously checked only
# projected SHARE at entry, missing markets where the opposite-side bid
# vanished after we filled. Diagnosed: 38 stranded positions sitting at
# "no_bid_to_sell_into" — engine had no exit path so they held to settle,
# costing $570-1140 of expected loss over 2-3 weeks.
#
# Gate: BOTH opposite-side bids must satisfy
#   bid_size  >= MIN_EXIT_CONTRACTS  AND
#   bid_price >= MIN_EXIT_PRICE_CENTS
# (LIP makers post both YES and NO; either side could fill, so both must
#  have an exit path.) Override per env: EXIT_GATE_ENABLED, MIN_EXIT_CONTRACTS,
# MIN_EXIT_PRICE_CENTS — read in run_paper.py at filter_by_depth call sites.
EXIT_GATE_ENABLED       = True
MIN_EXIT_CONTRACTS      = 50
MIN_EXIT_PRICE_CENTS    = 5

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

# 2026-05-01 Two-sided liquidity gate — minimum bid required on BOTH yes
# and no sides to enter. Prevents adverse-selection trap where we enter a
# market with one-sided sentiment (e.g., yes_bid=0.40 + no_bid=0c means
# nobody wants NO) and then can't exit because the other side has no
# buyers. 5c default = real two-sided demand confirmed.
# Predator gate: if we can't exit, we shouldn't enter.
MIN_TWO_SIDED_BID_CENTS = 5

# Minimum volume_24h to consider enrolling in a market.
MIN_MARKET_VOLUME_24H = 100

# ── Sizing relative to LIP TargetSize ─────────────────────────────────────
# Our quote size as a fraction of the market's TargetSize. Hitting ~0.25
# means we're contributing meaningfully to the size-at-best without
# dominating (and hurting our normalized score via self-dilution).
QUOTE_SIZE_AS_FRACTION_OF_TARGET = 0.20

# ── 2026-04-27 PER-SERIES SIZE MULTIPLIERS (audit-driven) ────────────────
# Concentrate cap on proven winners. Net PnL Apr 21-26 audit:
#   KXBRENTD: +$39.72  KXCORNW: +$37.30  KXCOPPERD: +$25.28
#   KXGOLDW:  +$21.48  KXCOCOAW: +$23.14
# 2x size = 2x rebate at full share, capped by per-market USD limit anyway.
SIZE_MULTIPLIER_BY_SERIES = {
    # 2026-04-29 Ship A: Niche Owner strategy. Boost commodities where we
    # have proven edge (futures_feed.py fair-value model). Goliaths skip
    # these pools (ops cost > yield); we own them. 7d settlement_log shows
    # CORNW $40, SOYBEANW $30, WHEATW $26, COCOAW $17, HOILW $8 — all NET+.
    "KXCORNW":      2.5,   # +$40 NET 7d (top earner)
    "KXSOYBEANW":   2.5,   # +$30 NET 7d
    "KXWHEATW":     2.5,   # +$26 NET 7d
    "KXCOCOAW":     2.5,   # +$17 NET 7d
    "KXBRENTD":     2.0,
    "KXBRENTW":     2.0,
    "KXCOPPERD":    2.0,
    "KXGOLDW":      2.0,
    "KXGOLDD":      1.5,
    # New additions — proven futures-fair models, low SIG presence:
    "KXHOILW":      1.8,   # heating oil, +$8 NET 7d
    "KXNATGASW":    1.8,   # natural gas — NEW, futures-tracked
    "KXNATGASD":    1.5,
    "KXDXYW":       1.5,   # dollar index — NEW, FX feed
    "KXTREASURIES": 1.5,   # rate markets — NEW
    "KXSILVERW":    1.3,   # silver weekly (KXSILVERD blocked separately)
    # ── ECONOMICS/MACRO — public data settlement (BLS/BEA/Fed) ──
    "KXCPI":        2.0,   # CPI prints, monthly
    "KXNFP":        2.0,   # nonfarm payrolls, monthly
    "KXGDP":        1.8,   # GDP releases
    "KXUNEMPLOY":   1.8,   # unemployment rate
    "KXPPI":        1.5,   # producer price index
    "KXFEDCHAIR":   1.5,   # confirmation outcomes (already on book)
    "KXFEDBALANCE": 1.5,   # balance sheet
    # ── CLIMATE — Kalshi weather variants (NYC/PHX/CHI daily highs) ──
    "KXHIGHTNY":    1.5,   # NYC high temp
    "KXHIGHTPHX":   1.5,   # PHX
    "KXHIGHTCHI":   1.5,   # CHI
    "KXHIGHTLA":    1.5,   # LA
    "KXLOWTNY":     1.5,
    "KXLOWTPHX":    1.5,
    "KXRAIN":       1.4,   # rainfall over threshold
    # ── NICHE POLICY/NEWS one-shots (proven) ──
    "KXMAMDANIEO":  1.8,   # +$12 NET 7d (one-shot political winner)
    "KXTRUMPACT":   1.5,   # +$5.59 NET 7d
    "KXKIMMELAPOLOGY": 1.3,
    "KXBILLSCOUNT": 1.3,
    # ── 2026-05-02 GAP FIX: crypto MAX/MIN monthly + rain monthly ──
    # Audit found 17 enrolled $400/d-pool series with ZERO snapshots.
    # Root cause: MarketYield.time_factor penalty (0.73 for 28-day) +
    # top_book_estimate=target*0.5=150 (real comp ~69) ranked them below
    # top-20 cutoff in production. Boost series_priority to clear the
    # gate. KXXRPMAXMON is the proven low-comp template (snaps confirmed).
    "KXBTCMAXMON":  2.5, "KXBTCMINMON":  2.5,
    "KXETHMAXMON":  2.5, "KXETHMINMON":  2.5,
    "KXSOLMAXMON":  2.5, "KXSOLMINMON":  2.5,
    "KXXRPMAXMON":  2.5, "KXXRPMINMON":  2.5,
    "KXBNBMAXMON":  2.5, "KXBNBMINMON":  2.5,
    "KXDOGEMAXMON": 2.5, "KXDOGEMINMON": 2.5,
    "KXHYPEMAXMON": 2.5, "KXHYPEMINMON": 2.5,
    "KXZECMAXMON":  2.5, "KXZECMINMON":  2.5,
    # Rain monthly cities — same low-comp greenfield pattern
    "KXRAINAUSM":   2.0, "KXRAINCHIM":   2.0, "KXRAINDALM":   2.0,
    "KXRAINDENM":   2.0, "KXRAINHOUM":   2.0, "KXRAINLAXM":   2.0,
    "KXRAINMIAM":   2.0, "KXRAINSEAM":   2.0, "KXRAINSFOM":   2.0,
}
DEFAULT_SIZE_MULTIPLIER = 1.0

# ── 2026-04-28 PER-SERIES PER-MARKET DOLLAR CAP OVERRIDES (#126) ──────────
# Per agent analysis: weekly commodity grains/softs (CORN/SOY/WHEAT/COCOA)
# have target_size=300. At default $40 cap × $0.50 mid = ~80 contracts =
# 27% of pool. At $90 cap = ~180 contracts = 60% of pool = ~2.3x rebate.
# These series proved profitable Apr 21-26 ($30/market net). Per-series
# cap raise concentrates capital where we WIN.
#
# Mechanics: when set, this override REPLACES MAX_GROSS_PER_MARKET_USD
# for that series in _passes_safety. Series not in map use the default.
# Series-level total cap (MAX_GROSS_PER_SERIES_USD) still applies.
MAX_GROSS_PER_MARKET_BY_SERIES = {
    "KXCORNW":     90.0,
    "KXSOYBEANW":  90.0,
    "KXWHEATW":    90.0,
    "KXCOCOAW":    90.0,
    "KXBRENTD":    90.0,   # daily Brent — also winner ($34 net Apr 21-26)
    "KXCOPPERD":   75.0,   # smaller average market, modest bump
    "KXGOLDD":     75.0,
    "KXGOLDW":     90.0,
    # ── 2026-05-02 GAP FIX: crypto MAX/MIN monthly per-market caps ──
    # Need ~150 contracts/side @ $0.50 = $75/market to materially shift
    # share when actual comp is 69 (XRP). Cap $90 fits 5 markets within
    # MAX_GROSS_PER_SERIES_USD budget, leaves room for layering.
    "KXBTCMAXMON":  90.0, "KXBTCMINMON":  90.0,
    "KXETHMAXMON":  90.0, "KXETHMINMON":  90.0,
    "KXSOLMAXMON":  90.0, "KXSOLMINMON":  90.0,
    "KXXRPMAXMON":  90.0, "KXXRPMINMON":  90.0,
    "KXBNBMAXMON":  90.0, "KXBNBMINMON":  90.0,
    "KXDOGEMAXMON": 90.0, "KXDOGEMINMON": 90.0,
    "KXHYPEMAXMON": 90.0, "KXHYPEMINMON": 90.0,
    "KXZECMAXMON":  90.0, "KXZECMINMON":  90.0,
}

# ── Prong 3: Cross-Venue Dislocation Harvester ───────────────────────────
# All scanner config lives in dislocation/config.py to keep prongs decoupled.
# Read it via `from dislocation.config import ...` (do NOT alias here — the
# prong has its own bankroll envelope so a Kalshi LIP draw-down doesn't
# implicitly pause convergence trades).
DISLOCATION_PRONG_ENABLED = os.getenv("DISLOCATION_ENABLED", "false").lower() == "true"
