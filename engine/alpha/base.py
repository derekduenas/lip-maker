"""Base AlphaPillar — every directional alpha strategy implements this.

Same architectural pattern as LIP capital_allocator: pure-Python data layer
that the Brain reads + writes against. Brain orchestrates trade decisions
via tiered authority (TIER 2: place trades up to $25 per trade auto, TIER 4:
larger trades require human approval).

ARCHITECTURE:
  AlphaPillar
    │
    ├── discover_markets()   — find Kalshi markets relevant to this pillar
    ├── fetch_external_data() — pull data sources (charts, forecasts, etc.)
    ├── compute_fair_prob()   — return (fair_probability, confidence)
    ├── find_edges()          — full pipeline: returns ranked Edge list
    └── kelly_size()          — fractional Kelly sizing

PIPELINE (every cycle, each pillar):
  1. discover_markets()        — what's tradeable
  2. fetch_external_data()     — what does the world look like
  3. for each market:
       fair_prob = compute_fair_prob(market, external)
       market_prob = market.yes_price / 100
       edge = abs(fair_prob - market_prob)
       if edge > MIN_EDGE_PCT: emit Edge object
  4. rank by EV (edge × payoff × confidence)
  5. persist to alpha_predictions table for Brain to act on

The Brain reads alpha_predictions, decides trade size + direction, executes.
"""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from config import settings


# Defaults — adjust per pillar
DEFAULT_MIN_EDGE_PCT = 0.05       # 5% minimum edge to surface
DEFAULT_KELLY_FRACTION = 0.25     # quarter-Kelly (conservative)
MAX_TRADE_USD_AUTO = 25.0          # TIER_2 auto-execute cap
MAX_TRADE_USD_HUMAN = 200.0        # absolute ceiling (HUMAN-APPROVE)


SCHEMA = """
CREATE TABLE IF NOT EXISTS alpha_predictions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  pillar          TEXT NOT NULL,        -- music/weather/sports/awards
  market_ticker   TEXT NOT NULL,
  series_ticker   TEXT,
  market_yes_price REAL,                -- market-implied prob (0-1)
  fair_prob       REAL NOT NULL,        -- our fair value
  edge_pct        REAL NOT NULL,        -- abs(fair - market)
  direction       TEXT NOT NULL,        -- yes/no
  confidence      TEXT NOT NULL,        -- low/medium/high
  recommended_usd REAL NOT NULL,        -- Kelly-sized
  features_json   TEXT,                 -- inputs to fair_prob calc
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending/executed/expired/passed
  brain_decision_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alpha_pred_status ON alpha_predictions(status);
CREATE INDEX IF NOT EXISTS idx_alpha_pred_ticker ON alpha_predictions(market_ticker);
CREATE INDEX IF NOT EXISTS idx_alpha_pred_ts     ON alpha_predictions(ts);

CREATE TABLE IF NOT EXISTS alpha_trades (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  prediction_id   INTEGER,
  pillar          TEXT NOT NULL,
  market_ticker   TEXT NOT NULL,
  side            TEXT NOT NULL,         -- yes/no
  count           INTEGER NOT NULL,
  entry_price     REAL NOT NULL,         -- price in cents
  cost_usd        REAL NOT NULL,
  status          TEXT NOT NULL DEFAULT 'open',  -- open/closed/expired
  exit_price      REAL,
  exit_ts         TEXT,
  realized_usd    REAL,
  notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_alpha_trades_status ON alpha_trades(status);
CREATE INDEX IF NOT EXISTS idx_alpha_trades_ticker ON alpha_trades(market_ticker);
"""


@dataclass
class Edge:
    """One actionable edge — a market where our fair value diverges from the
    market price enough to bet."""
    pillar:           str
    market_ticker:    str
    series_ticker:    str
    market_yes_price: float
    fair_prob:        float
    edge_pct:         float       # signed: positive if fair > market (bet YES)
    confidence:       str         # low/medium/high
    recommended_usd:  float
    features:         dict = field(default_factory=dict)

    @property
    def direction(self) -> str:
        return "yes" if self.edge_pct > 0 else "no"

    @property
    def expected_value_usd(self) -> float:
        """Per-trade EV: edge × position size × payoff. Simplified."""
        edge_abs = abs(self.edge_pct)
        # Payoff = 100 - entry_price (in cents), per contract
        if self.direction == "yes":
            payoff_per = 1.0 - self.market_yes_price
        else:
            payoff_per = self.market_yes_price
        n_contracts = self.recommended_usd / max(0.01, payoff_per)
        return edge_abs * n_contracts * payoff_per


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def kelly_size(edge_pct: float, market_prob: float, bankroll: float,
               fraction: float = DEFAULT_KELLY_FRACTION,
               max_per_trade: float = MAX_TRADE_USD_AUTO) -> float:
    """Fractional Kelly position sizing for binary outcome.

    Full Kelly: f* = edge / payoff_odds
    For binary at price p: f* = (p_win × (1/p - 1) - (1 - p_win)) / (1/p - 1)
    Simplified for our case: f* ≈ edge_pct / payoff_ratio
    Quarter-Kelly to avoid overbetting.
    """
    abs_edge = abs(edge_pct)
    if abs_edge <= 0 or market_prob <= 0 or market_prob >= 1:
        return 0.0
    # Position size as fraction of bankroll
    payoff_ratio = (1 - market_prob) / market_prob if edge_pct > 0 else market_prob / (1 - market_prob)
    if payoff_ratio <= 0:
        return 0.0
    full_kelly_frac = abs_edge / payoff_ratio
    sized = full_kelly_frac * fraction * bankroll
    return min(max_per_trade, max(0.0, sized))


class AlphaPillar:
    """Base class for any directional alpha strategy.

    Subclasses MUST implement:
      - name (class attribute)
      - discover_markets()
      - fetch_external_data()
      - compute_fair_prob(market, external_data) → (fair_prob, confidence_str)
    """
    name: str = "abstract"
    min_edge_pct: float = DEFAULT_MIN_EDGE_PCT

    def __init__(self, db_path: str = settings.DB_PATH,
                 bankroll_usd: float = 200.0):
        self.db_path = db_path
        self.bankroll_usd = bankroll_usd
        ensure_schema(db_path)

    # ── To be overridden ──

    def discover_markets(self) -> list[dict]:
        """Return list of market dicts to evaluate. Pillar-specific filter."""
        raise NotImplementedError

    def fetch_external_data(self) -> dict:
        """Pull pillar-specific external data. Cached aggressively."""
        raise NotImplementedError

    def compute_fair_prob(self, market: dict, external: dict) -> tuple[float, str]:
        """Return (fair_probability_yes_in_0_1, confidence_string)."""
        raise NotImplementedError

    # ── Concrete pipeline ──

    # Defaults — pillars override
    max_spread_cents = 30           # liquidity filter
    min_volume_24h = 0              # min trading activity
    min_confidence_to_persist = "low"  # confidence floor: low/medium/high

    def find_edges(self) -> list[Edge]:
        """Run full pipeline: discover → fair_prob → rank by edge.

        Filters applied (in order):
          1. Liquidity: bid-ask spread <= max_spread_cents
          2. Volume: volume_24h >= min_volume_24h
          3. Edge: |fair - market| >= min_edge_pct
          4. Confidence: >= min_confidence_to_persist
          5. Sizing: Kelly result >= $1
        """
        external = self.fetch_external_data()
        markets = self.discover_markets()
        edges: list[Edge] = []
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        min_conf_rank = confidence_rank.get(self.min_confidence_to_persist, 0)
        for m in markets:
            # 1. Liquidity gate — skip no-info / wide-spread markets
            yes_bid = float(m.get("yes_bid", 0))
            yes_ask = float(m.get("yes_ask", 100))
            spread = yes_ask - yes_bid
            if spread > self.max_spread_cents:
                continue
            # 2. Volume gate
            if int(m.get("volume", 0)) < self.min_volume_24h:
                continue
            try:
                fair_prob, confidence = self.compute_fair_prob(m, external)
            except Exception:
                continue
            if not (0 < fair_prob < 1):
                continue
            # 4. Confidence floor
            if confidence_rank.get(confidence, 0) < min_conf_rank:
                continue
            market_prob = float(m.get("yes_price", 50)) / 100.0
            edge_pct = fair_prob - market_prob
            # 3. Edge threshold
            if abs(edge_pct) < self.min_edge_pct:
                continue
            sized = kelly_size(edge_pct, market_prob, self.bankroll_usd)
            if sized < 1.0:
                continue
            edges.append(Edge(
                pillar=self.name,
                market_ticker=m.get("ticker", ""),
                series_ticker=m.get("series_ticker", ""),
                market_yes_price=market_prob,
                fair_prob=fair_prob,
                edge_pct=edge_pct,
                confidence=confidence,
                recommended_usd=round(sized, 2),
                features={"market": m.get("title", ""),
                          "yes_bid": yes_bid, "yes_ask": yes_ask,
                          "spread_cents": spread,
                          "volume_24h": m.get("volume", 0)},
            ))
        edges.sort(key=lambda e: -abs(e.expected_value_usd))
        return edges

    def persist_predictions(self, edges: list[Edge]) -> int:
        """Write edges to alpha_predictions for Brain to action on."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        now = datetime.now(timezone.utc).isoformat()
        n = 0
        try:
            import json as _json
            for e in edges:
                conn.execute(
                    """INSERT INTO alpha_predictions
                       (ts, pillar, market_ticker, series_ticker,
                        market_yes_price, fair_prob, edge_pct, direction,
                        confidence, recommended_usd, features_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (now, e.pillar, e.market_ticker, e.series_ticker,
                     e.market_yes_price, e.fair_prob, e.edge_pct, e.direction,
                     e.confidence, e.recommended_usd,
                     _json.dumps(e.features, default=str)),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
        return n


def report(db_path: str = settings.DB_PATH) -> None:
    """CLI: show recent edges + open trades across all pillars."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        print("\n=== ALPHA EDGES (pending, last 24h) ===")
        for r in conn.execute("""
            SELECT pillar, market_ticker, ROUND(fair_prob*100,1) fair_pct,
                   ROUND(market_yes_price*100,1) market_pct,
                   ROUND(edge_pct*100,2) edge_pct,
                   confidence, ROUND(recommended_usd,2) usd,
                   datetime(ts) ts
            FROM alpha_predictions
            WHERE status = 'pending'
              AND datetime(ts) > datetime('now','-1 day')
            ORDER BY ABS(edge_pct) DESC LIMIT 20
        """):
            print(f"  [{r[0]:<8}] {r[1][:35]:<37} fair={r[2]}% mkt={r[3]}% "
                  f"edge={r[4]:+.2f}% {r[5]:<6} ${r[6]:>5.2f}  {r[7]}")

        print("\n=== ALPHA TRADES (open) ===")
        for r in conn.execute("""
            SELECT pillar, market_ticker, side, count, ROUND(entry_price,1) entry,
                   ROUND(cost_usd,2) cost, datetime(ts) ts
            FROM alpha_trades WHERE status = 'open' ORDER BY ts DESC LIMIT 20
        """):
            print(f"  [{r[0]:<8}] {r[1][:35]:<37} {r[2]}  ct={r[3]:>4} "
                  f"@{r[4]}c ${r[5]:>5.2f}  {r[6]}")
    finally:
        conn.close()


if __name__ == "__main__":
    report()
