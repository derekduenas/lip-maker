"""Sniper market selection — concentrate capital on highest-EV pools.

REPLACES top_n_to_quote() in the runner. Goal: stop fanning out to 100
markets where we're 1-of-1500 LPs sharing pennies. Instead pick the 15-25
markets with the highest expected NET dollars/day, where we have a real
chance of capturing meaningful pool share.

THE LOGIC:
  1. Run competitor_density.scan() to get every enrolled market ranked
     by net_per_day = reward × observed_share × qual_rate - adverse_cost
  2. Drop any market below MIN_NET_PER_DAY threshold (don't waste WS
     bandwidth or capital on micro-rewards)
  3. Cap at MAX_MARKETS to keep capital concentrated
  4. EXPLORE budget: reserve N slots for "prior" markets (no observation
     yet) so we keep testing new opportunities — not 100% exploit

WHY:
  Without this filter, top_n_to_quote returns 100 markets ranked by raw
  reward. Most of those have 1000+ competitors per the snapshot data.
  We end up with 80% of our capital on negative-EV markets and 20% on
  the few proven winners. Sniper inverts that.

USAGE:
  Same return shape as engine.lip_discovery.top_n_to_quote() — drop-in
  replacement at run_paper.py:643.
"""
from __future__ import annotations

import logging

from config import settings
from tools.competitor_density import scan as _scan_density

_log = logging.getLogger(__name__)


def top_n_by_ev(n: int = 25,
                min_net_per_day: float = 0.10,
                explore_budget: int = 5,
                db_path: str = settings.DB_PATH) -> list[dict]:
    """Return up to N markets ranked by expected NET dollars per day.

    Args:
      n: max markets to return (capital concentration)
      min_net_per_day: drop markets below this EV threshold
      explore_budget: of the N slots, reserve this many for "prior"-confidence
                      markets (no observation yet) so we keep discovering new
                      opportunities. Set to 0 for pure exploit.

    Returns: list of dicts in same shape as lip_discovery.top_n_to_quote(),
    plus bonus EV fields the runner can log/use:
      expected_net_per_day, observed_share, competitor_est, confidence.
    """
    scanned = _scan_density(db_path=db_path)
    above_threshold = [m for m in scanned if m["net_per_day"] >= min_net_per_day]

    if explore_budget > 0 and explore_budget < n:
        # Split: top exploit slots + reserved explore slots
        exploit_n = n - explore_budget
        exploit = [m for m in above_threshold
                   if m["confidence"] in ("observed", "series")][:exploit_n]
        explore = [m for m in above_threshold
                   if m["confidence"] == "prior"][:explore_budget]
        # If exploit didn't fill its slots, take more from explore
        if len(exploit) < exploit_n:
            additional = [m for m in above_threshold
                          if m["confidence"] == "prior"
                          and m not in explore][:exploit_n - len(exploit)]
            exploit.extend(additional)
        selected = exploit + explore
    else:
        selected = above_threshold[:n]

    # Re-sort final selection by net_per_day for consistent ordering
    selected.sort(key=lambda m: -m["net_per_day"])

    _log.info(f"sniper_select: {len(selected)}/{len(scanned)} markets selected "
              f"(threshold=${min_net_per_day}/d, max={n}, explore={explore_budget})")
    if selected:
        total_ev = sum(m["net_per_day"] for m in selected)
        _log.info(f"sniper_select: aggregate expected ${total_ev:.2f}/day "
                  f"({sum(1 for m in selected if m['confidence']=='observed')} observed, "
                  f"{sum(1 for m in selected if m['confidence']=='series')} series, "
                  f"{sum(1 for m in selected if m['confidence']=='prior')} prior)")

    # Convert to top_n_to_quote shape (drop-in compatible)
    return [
        {
            "market_ticker":         m["market_ticker"],
            "series_ticker":         m["series_ticker"],
            "reward_per_day_usd":    m["reward_per_day"],
            "target_size":           m["target_size"],
            "discount_factor":       m["discount_factor"],
            "start_date":            "",
            "end_date":              m["end_date"],
            # Bonus fields — runner can log these
            "expected_net_per_day":  m["net_per_day"],
            "observed_share":        m["share"],
            "competitor_est":        m["competitor_est"],
            "confidence":            m["confidence"],
        }
        for m in selected
    ]
