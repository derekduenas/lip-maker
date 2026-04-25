"""Directional exposure tracker — per-basket net long/short visibility.

Purpose: catch moments when we're accidentally taking a big directional bet
across correlated markets. At Phase 1 this is bounded by $15/market caps, but
as phases advance and we quote more markets, concentration risk grows.

Example of the risk:
  - Quoted YES on 5 WHEAT strikes + 3 CORN strikes + 2 SOYBEAN strikes
  - All 10 positions bet grain complex goes UP
  - Single grain-market-wide news event (USDA WASDE, drought, etc.)
    hits all 10 positions simultaneously
  - What looked like diversification (many markets) was actually CONCENTRATION
    (one directional bet across a basket)

Solution: classify each position by basket, compute net long vs short $, alert
when concentration exceeds threshold.

Output format compatible with dashboard integration.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_auth import KalshiClient

_log = logging.getLogger(__name__)


# ── Basket taxonomy ────────────────────────────────────────────────────

BASKETS: dict[str, list[str]] = {
    "GRAIN":     ["KXCORNW", "KXWHEATW", "KXSOYBEANW", "KXCORND", "KXWHEATD"],
    "SOFTS":     ["KXCOCOAW", "KXCOFFEEW", "KXSUGARW"],
    "ENERGY":    ["KXBRENTD", "KXBRENTW", "KXCRUDE", "KXHOILW", "KXNATGASD", "KXNATGASW"],
    "METALS":    ["KXGOLDD", "KXGOLDW", "KXSILVERD", "KXSILVERW",
                  "KXCOPPERD", "KXCOPPERW"],
    "LIVESTOCK": ["KXLCATTLEW", "KXLHOGW", "KXFEEDERW"],
    "CRYPTO":    ["KXBTC", "KXETH", "KXSOL", "KXADA", "KXXRP"],
    "POLITICAL": ["KXTRUMP", "KXCAPRIMARY", "KXAIPAC", "KXCAGOV",
                  "KXLAMAYOR", "KXISRAEL", "KXEXPEL", "KXPRESSBRIEF",
                  "KXVOTEHUB", "KXMAMDANI", "KXGENERIC", "KXEOWEEK",
                  "KXAPRPOTUS"],
    "SPORTS":    ["KXMLB", "KXNFL", "KXNBA", "KXNHL", "KXUFC",
                  "KXSOCCER", "KXTENNIS", "KXGOLF", "KXNCAA"],
    "WEATHER":   ["KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXTEMP"],
}

# Per-basket alert thresholds as fraction of Phase gross budget.
# Phase 1 = $200 gross → $60 alert per basket (30% concentration).
# Phase 4 = $800 gross → $240 alert per basket.
CONCENTRATION_THRESHOLD_PCT = 0.30


# ── Classification helpers ─────────────────────────────────────────────

def classify_basket(ticker: str) -> str:
    """Map a Kalshi ticker to its commodity/category basket."""
    for basket, prefixes in BASKETS.items():
        for prefix in prefixes:
            if ticker.startswith(prefix):
                return basket
    return "OTHER"


def direction_sign(ticker: str, net_contracts: int) -> int:
    """Interpret position as LONG underlying (+1), SHORT underlying (-1),
    or UNDIRECTED (0).

    Kalshi commodity markets use "Will X close above Y" format:
      - YES = underlying > strike = bet up = +1 (long)
      - NO  = underlying < strike = bet down = -1 (short)
    Non-strike markets (candidate-based political, sports) have no
    numeric direction — return 0.

    net_contracts > 0 means long YES; < 0 means long NO.
    """
    # Numeric strike check — commodity format: TICKER-DATE-T<number>
    has_numeric_strike = bool(re.search(r"-T[\d.]+$", ticker))
    if not has_numeric_strike:
        return 0
    return +1 if net_contracts > 0 else -1


# ── Core exposure calculation ──────────────────────────────────────────

@dataclass
class BasketExposure:
    basket:          str
    position_count:  int
    net_long_usd:    float       # sum exposure on LONG-side positions
    net_short_usd:   float       # sum exposure on SHORT-side positions
    net_directional: float       # net_long - net_short (signed)
    gross_usd:       float       # abs(long) + abs(short)
    undirected_usd:  float       # positions with no clear direction (political, etc.)
    positions:       list[dict] = field(default_factory=list)


def compute_exposure(db_path: str = settings.DB_PATH) -> dict[str, BasketExposure]:
    """Pull current Kalshi positions and compute per-basket directional exposure."""
    k = KalshiClient()
    cursor = None
    all_positions = []
    for _ in range(10):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = k.get("/portfolio/positions", params=params)
        all_positions.extend(r.get("market_positions", []))
        cursor = r.get("cursor")
        if not cursor:
            break

    by_basket: dict[str, BasketExposure] = {}
    for p in all_positions:
        net = int(float(p.get("position_fp", "0") or 0))
        if net == 0:
            continue
        ticker = p.get("ticker", "")
        exposure = float(p.get("market_exposure_dollars", "0") or 0)
        if exposure == 0:
            continue

        basket = classify_basket(ticker)
        sign = direction_sign(ticker, net)

        if basket not in by_basket:
            by_basket[basket] = BasketExposure(
                basket=basket, position_count=0,
                net_long_usd=0.0, net_short_usd=0.0,
                net_directional=0.0, gross_usd=0.0,
                undirected_usd=0.0,
            )
        be = by_basket[basket]
        be.position_count += 1
        be.gross_usd += exposure

        if sign > 0:
            be.net_long_usd += exposure
        elif sign < 0:
            be.net_short_usd += exposure
        else:
            be.undirected_usd += exposure

        be.positions.append({
            "ticker":   ticker,
            "net":      net,
            "side":     "yes" if net > 0 else "no",
            "exposure": exposure,
            "sign":     sign,
        })

    for be in by_basket.values():
        be.net_directional = be.net_long_usd - be.net_short_usd

    return by_basket


def find_concentrations(exposures: dict[str, BasketExposure],
                        bankroll_usd: Optional[float] = None,
                        phase: int = 1) -> list[dict]:
    """Alert on any basket with concentration > threshold.

    Phase gross budget = bankroll × {1:0.10, 2:0.20, 3:0.30, 4:0.40}.
    Concentration threshold = 30% of gross budget per basket.
    """
    if bankroll_usd is None:
        # Default to live Kalshi balance (most accurate vs env-var fallback $80)
        try:
            bankroll_usd = KalshiClient().get_balance()
        except Exception:
            bankroll_usd = settings.BANKROLL_USD

    phase_frac = {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.40}.get(phase, 0.40)
    gross_budget = bankroll_usd * phase_frac
    threshold = gross_budget * CONCENTRATION_THRESHOLD_PCT

    alerts = []
    for basket, be in exposures.items():
        # Only alert on DIRECTIONAL concentration (commodity/crypto).
        # Political/sports are always undirected — skip their "alert" path.
        net_abs = abs(be.net_directional)
        if net_abs > threshold:
            alerts.append({
                "basket":          basket,
                "net_directional": be.net_directional,
                "threshold":       threshold,
                "position_count":  be.position_count,
                "severity":        "🔴" if net_abs > threshold * 1.5 else "🟡",
            })
    return alerts


def summary(db_path: str = settings.DB_PATH) -> dict:
    exposures = compute_exposure(db_path=db_path)
    phase = int(__import__("os").getenv("LIP_RAMP_PHASE", "1"))
    # Pull live balance for accurate threshold calc
    try:
        bankroll = KalshiClient().get_balance()
    except Exception:
        bankroll = settings.BANKROLL_USD
    alerts = find_concentrations(exposures, bankroll_usd=bankroll, phase=phase)

    total_gross = sum(be.gross_usd for be in exposures.values())
    total_long  = sum(be.net_long_usd for be in exposures.values())
    total_short = sum(be.net_short_usd for be in exposures.values())

    phase_frac = {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.40}.get(phase, 0.40)
    return {
        "baskets":        exposures,
        "alerts":         alerts,
        "total_gross":    round(total_gross, 2),
        "total_long":     round(total_long, 2),
        "total_short":    round(total_short, 2),
        "net_portfolio":  round(total_long - total_short, 2),
        "bankroll":       round(bankroll, 2),
        "phase":          phase,
        "gross_budget":   round(bankroll * phase_frac, 2),
        "basket_threshold": round(bankroll * phase_frac * CONCENTRATION_THRESHOLD_PCT, 2),
    }


# ── CLI / display ──────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    argparse.ArgumentParser().parse_args()

    s = summary()
    print(f"\n══ DIRECTIONAL EXPOSURE TRACKER ══")
    print(f"  bankroll: ${s['bankroll']:.2f}  phase: {s['phase']}  "
          f"gross budget: ${s['gross_budget']:.2f}  basket threshold: ±${s['basket_threshold']:.2f}")
    print(f"  total gross exposure: ${s['total_gross']:.2f}")
    print(f"  portfolio net (long-short): ${s['net_portfolio']:+.2f}")
    print(f"  (long=${s['total_long']:.2f}, short=${s['total_short']:.2f})")

    if s['alerts']:
        print(f"\n  🔴 CONCENTRATION ALERTS ({len(s['alerts'])}):")
        for a in s['alerts']:
            print(f"    {a['severity']} {a['basket']:12s}  "
                  f"net=${a['net_directional']:+7.2f}  "
                  f"(threshold ±${a['threshold']:.2f}, {a['position_count']} positions)")

    print(f"\n  {'basket':12s}  {'net_dir':>9s}  {'long':>7s}  {'short':>7s}  {'undir':>7s}  pos")
    print(f"  {'-'*56}")
    for basket, be in sorted(s['baskets'].items(), key=lambda x: -abs(x[1].net_directional)):
        print(f"  {basket:12s}  ${be.net_directional:>+7.2f}  "
              f"${be.net_long_usd:>5.2f}  ${be.net_short_usd:>5.2f}  "
              f"${be.undirected_usd:>5.2f}  {be.position_count:>3}")


if __name__ == "__main__":
    main()
