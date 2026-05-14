"""Capital-aware portfolio selector — replaces fixed top-N with greedy yield fill.

THE PROBLEM:
  top_n_to_quote(n=40) returns 40 markets but our $800 budget only fits ~5
  markets at qualifying size. Result: 35 markets fail safety gates silently,
  picked-but-unquoted. The funnel showed this clearly.

THE PHYSICS:
  Per-market expected daily rebate (LIP):
    E[rebate_d] = pool_d × calibration × share × qualify_prob - adverse_cost

  qualify_prob is a CLIFF — zero unless (our_size + comp ≥ target_size).
  So the optimal per-market size is:

    optimal_size = max(MIN_SIZE, target_size - real_competition + buffer)

  Just enough to cross the cliff. Above that = diminishing share returns.

  Capital per market: optimal_size × midpoint × 2_sides
  Yield-per-dollar:   E[rebate_d] / capital_per_market

  Aggregate: greedy-fill markets sorted by yield-per-dollar until capital
  exhausted. N becomes OUTPUT (could be 5 or 50), driven by opportunity quality.

USAGE:
    from engine.capital_allocator import select_optimal_portfolio
    markets = select_optimal_portfolio(budget_usd=800)
    # Each dict has the original lip_programs fields PLUS:
    #   optimal_size_per_side, capital_per_market_usd, expected_net_per_day,
    #   competition_yes, competition_no, yield_pct_daily, confidence
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from config import settings
try:
    from cross_venue.yield_equation import MarketYield, KALSHI_CALIB, kalshi_calib_for
except ImportError:
    from engine.yield_equation import MarketYield, KALSHI_CALIB, kalshi_calib_for

# Offense O.2 (2026-05-14): import hedge-eligibility lookup. When a market
# has a continuous-hedge counterpart AND AUTO_HEDGE_ENABLED, MarketYield's
# adverse_cost is reduced (cf. cross_venue/yield_equation.py:HEDGED_ADVERSE_FACTOR).
# This makes hedge-eligible markets rank higher in the allocator + earn
# bigger size since the inventory risk is neutralized by the hedge.
try:
    from cross_venue.market_match import is_hedge_eligible as _is_hedge_eligible
except ImportError:
    def _is_hedge_eligible(_t: str) -> bool:
        return False

_log = logging.getLogger(__name__)

# Buffer over (target - comp) to ensure we cross qualify cliff comfortably.
QUALIFY_BUFFER_CONTRACTS = 30
# Default midpoint when we can't measure book.
DEFAULT_MIDPOINT = 0.50
# Min markets we'll always at least consider (avoids returning zero on
# greedy starvation).
MIN_PORTFOLIO_SIZE = 3
# Max markets cap — sanity ceiling.
MAX_PORTFOLIO_SIZE = 60
# 2026-05-03: daily-market bias. Markets closing in <36h give same-day
# rebate accrual feedback (vs ~30d wait on monthlies). Reserve a fraction
# of budget for daily-cadence markets so we get faster signal-vs-reality
# reconciliation. Default 40% — half capital on each cadence.
DAILY_QUOTA_FRACTION = 0.40
DAILY_HOURS_THRESHOLD = 36


def _real_competition(db_path: str) -> dict[str, dict]:
    """Return per-ticker observed competition (yes/no contracts at-best).

    Uses lip_snapshots over last 24h. For each market, returns:
      {ticker: {yes_comp, no_comp, snaps, qual_rate, observed_share}}

    competition_size = avg(total_score - our_score). This is what we'd
    need to OVERCOME on each side to qualify, in contracts (since
    score = size when at-best with DF^0).
    """
    out: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        rows = conn.execute("""
            SELECT market_ticker,
                   ROUND(AVG(MAX(total_score - our_score, 0)), 1) AS comp,
                   ROUND(AVG(our_score), 1) AS ours,
                   COUNT(*) AS snaps,
                   ROUND(SUM(snapshot_valid)*1.0/COUNT(*), 3) AS qual_rate,
                   ROUND(AVG(CASE WHEN total_score > 0
                              THEN our_score*1.0/total_score ELSE 0 END), 4) AS share
            FROM lip_snapshots
            WHERE datetime(captured_at) > datetime('now','-1 day')
            GROUP BY market_ticker
            HAVING snaps >= 5
        """).fetchall()
        conn.close()
        for r in rows:
            out[r[0]] = {
                "comp":         float(r[1] or 0),
                "ours":         float(r[2] or 0),
                "snaps":        int(r[3] or 0),
                "qual_rate":    float(r[4] or 0),
                "obs_share":    float(r[5] or 0),
            }
    except Exception as e:
        _log.warning(f"_real_competition query failed: {e}")
    return out


def _series_competition_prior(db_path: str) -> dict[str, float]:
    """Series-level fallback when no per-ticker data."""
    out: dict[str, float] = {}
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        rows = conn.execute("""
            SELECT substr(market_ticker,1,instr(market_ticker||'-','-')-1) AS series,
                   ROUND(AVG(MAX(total_score - our_score, 0)), 1) AS comp
            FROM lip_snapshots
            WHERE datetime(captured_at) > datetime('now','-7 days')
            GROUP BY series HAVING COUNT(*) >= 30
        """).fetchall()
        conn.close()
        for r in rows:
            out[r[0]] = float(r[1] or 0)
    except Exception:
        pass
    return out


def _enrolled_universe(db_path: str, max_target_size: int) -> list[tuple]:
    """Return enrolled, alive, non-blacklisted markets."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        return conn.execute(
            """SELECT p.market_ticker, p.series_ticker, p.reward_per_day_usd,
                      p.target_size, p.discount_factor, p.end_date
               FROM lip_programs p
               LEFT JOIN market_blacklist b
                 ON p.market_ticker = b.ticker
                 AND datetime(b.expires_at) > datetime('now')
               WHERE p.enrolled = 1 AND p.paid_out = 0
                 AND p.target_size <= ?
                 AND datetime(p.end_date) > datetime('now')
                 AND b.ticker IS NULL""",
            (max_target_size,),
        ).fetchall()
    finally:
        conn.close()


def _optimal_size(target_size: int, competition: float) -> int:
    """How many contracts per side to JUST cross the qualify cliff."""
    needed = max(0, target_size - int(competition)) + QUALIFY_BUFFER_CONTRACTS
    return max(settings.MIN_QUOTE_SIZE_CONTRACTS, needed)


def _kelly_position_mult(edge_proxy: float, confidence: str,
                         fraction: float = 0.25,
                         lo: float = 0.5, hi: float = 1.5) -> float:
    """Quarter-Kelly-style size multiplier.

    edge_proxy = series_priority × series_calibration² × setup_mult.
    Typical range: 0.05 (crushed loser) to 4.0+ (proven winner).

    Returns mult in [lo, hi]:
      - blind_prior confidence → forced to lo (probe only, never press)
      - edge ≥ 4.0 → hi (full press, capped at hi)
      - edge ≤ 0.2 → lo (deep shrink)
      - linear interpolation in between, anchored at edge=1.0 → 1.0
    """
    if confidence == "blind_prior":
        return lo
    edge = max(0.0, float(edge_proxy))
    if edge >= 4.0:
        return hi
    if edge <= 0.2:
        return lo
    if edge >= 1.0:
        # interpolate 1.0 → hi over edge 1.0 → 4.0
        return 1.0 + (hi - 1.0) * min(1.0, (edge - 1.0) / 3.0)
    # interpolate lo → 1.0 over edge 0.2 → 1.0
    return lo + (1.0 - lo) * (edge - 0.2) / 0.8



def _hours_to_settle(end_iso: str) -> float:
    """Hours until close. Best-effort; defaults to 24."""
    if not end_iso:
        return 24.0
    try:
        from datetime import datetime, timezone
        ed = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return max(0.5, (ed - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 24.0


def select_optimal_portfolio(
    *,
    budget_usd: Optional[float] = None,
    max_target_size: int = 2500,
    db_path: str = settings.DB_PATH,
    exclude_tickers: Optional[set[str]] = None,
    midpoint_default: float = DEFAULT_MIDPOINT,
    explore_fraction: float = 0.20,
) -> list[dict]:
    """Greedy capital-fill ranker with explore/exploit split.

    Budget allocation:
      - EXPLOIT pool (1 - explore_fraction): markets with `observed` or
        `series_prior` confidence get full optimal_size_per_side. These are
        proven and earn full rebate.
      - EXPLORE pool (explore_fraction): markets with `blind_prior`
        confidence get MIN_QUOTE_SIZE (cheap probes) to collect snapshot
        data. After 1-2 days they get promoted to series_prior or pruned.

    This breaks the chicken-and-egg: we test new markets cheaply rather
    than overweighting them on assumed-comp priors.

    Returns markets sorted by yield-per-dollar with `optimal_size_per_side`
    attached. Stops when cumulative capital_per_market exceeds budget_usd.
    """
    budget_usd = budget_usd or settings.MAX_TOTAL_GROSS_USD
    exclude_tickers = exclude_tickers or set()
    exploit_budget = budget_usd * (1.0 - explore_fraction)
    explore_budget = budget_usd * explore_fraction

    universe = _enrolled_universe(db_path, max_target_size)
    real_comp = _real_competition(db_path)
    series_comp_prior = _series_competition_prior(db_path)
    multipliers = settings.SIZE_MULTIPLIER_BY_SERIES
    default_priority = settings.DEFAULT_SIZE_MULTIPLIER

    candidates: list[dict] = []
    for r in universe:
        ticker, series, pool_d, target, df, end_date = r
        if not ticker or ticker in exclude_tickers:
            continue
        target = int(target or 0)
        df = float(df or 0.5)
        pool_d = float(pool_d or 0)
        if pool_d <= 0 or target <= 0:
            continue

        # 1. REAL competition (per-market preferred, series-prior fallback)
        rc = real_comp.get(ticker)
        if rc and rc["snaps"] >= 5:
            comp = rc["comp"]
            confidence = "observed"
            obs_share = rc["obs_share"]
        elif series and series in series_comp_prior:
            comp = series_comp_prior[series]
            confidence = "series_prior"
            obs_share = None
        else:
            comp = target * 0.4  # neutral prior — could be greenfield or saturated
            confidence = "blind_prior"
            obs_share = None

        # 2. Optimal size — full qualify-cross for proven, MIN for explore
        if confidence == "blind_prior":
            optimal_size = settings.MIN_QUOTE_SIZE_CONTRACTS
        else:
            optimal_size = _optimal_size(target, comp)

        # 3. Capital per market
        capital = optimal_size * midpoint_default * 2  # both sides

        # 4. Expected rebate via the canonical yield equation, multiplied by
        #    LEARNED calibration (per-series share calibration) and SETUP
        #    QUALITY SCORE (KNN against historical paying setups).
        #    The setup quality score is the key autonomy: instead of hard-
        #    coded series favorites, each candidate is scored by similarity
        #    to PROVEN-PAYING setups. New series get instant credit if they
        #    match the pattern of past winners. No whitelist, no manual list.
        priority = multipliers.get(series or "", default_priority)
        # 2026-05-04 BRAIN AUTHORITY: read runtime_overrides written by Brain.
        # Brain can soft-demote (factor < 1.0) or boost (factor > 1.0) any series
        # based on its analysis. Overrides expire after 24h unless renewed.
        try:
            with sqlite3.connect(db_path, timeout=5.0) as _ovconn:
                row = _ovconn.execute(
                    """SELECT value_json FROM runtime_overrides
                       WHERE series_ticker = ? AND override_type = 'size_mult'
                         AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))""",
                    (series or "",),
                ).fetchone()
                if row:
                    import json as _json
                    override_mult = float(_json.loads(row[0]).get("multiplier", 1.0))
                    priority *= override_mult
        except Exception:
            pass
        try:
            from tools.auto_calibrate import get_calibration
            raw_cal = get_calibration(series, db_path)
            # 2026-05-03 Brain recommendation #3: square calibration ratio so
            # high-confidence winners get DISPROPORTIONATELY more weight and
            # confirmed losers (samples ≥ 500 and ratio < 0.20) get crushed.
            # Linear calibration gave KXEOWEEK (ratio 0.15) too much benefit
            # of the doubt despite 3,594-sample evidence. Squared math:
            #   ratio 1.0 → 1.00 (no change for solid baseline)
            #   ratio 0.89 → 0.79 (slight discount on KXFEATUREDRAKE)
            #   ratio 0.50 → 0.25 (real discount)
            #   ratio 0.15 → 0.0225 (crushes confirmed losers)
            series_cal = raw_cal * raw_cal
        except Exception:
            series_cal = 1.0
        # Setup recognition: KNN over historical outcomes
        try:
            from engine.setup_recognition import (
                features_from_market, setup_quality_score,
            )
            hours_to_settle_now = _hours_to_settle(end_date)
            feats = features_from_market(
                pool_per_day=pool_d, target_size=target, discount_factor=df,
                hours_to_settle=hours_to_settle_now, observed_competition=comp,
            )
            setup_score = setup_quality_score(feats, db_path=db_path)
            setup_mult = setup_score["score"]
        except Exception as e:
            _log.debug(f"setup_quality failed for {ticker}: {e}")
            setup_mult = 1.0
            setup_score = {"score": 1.0, "neighbors": 0, "confidence": "error"}

        # === Sprint 4 #1: Kelly fractional sizing ===
        pre_kelly_size = optimal_size
        kelly_mult = 1.0
        if getattr(settings, "KELLY_SIZING_ENABLED", False):
            edge_proxy = priority * series_cal * setup_mult
            kelly_mult = _kelly_position_mult(
                edge_proxy, confidence,
                fraction=getattr(settings, "KELLY_FRACTION", 0.25),
                lo=getattr(settings, "KELLY_MIN_MULT", 0.5),
                hi=getattr(settings, "KELLY_MAX_MULT", 1.5),
            )
            target_cap = int(target * getattr(settings, "KELLY_TARGET_HEADROOM", 1.5))
            optimal_size = max(
                settings.MIN_QUOTE_SIZE_CONTRACTS,
                min(target_cap, int(round(optimal_size * kelly_mult)))
            )
            capital = optimal_size * midpoint_default * 2  # recompute

        try:
            # Offense O.1: per-market EWMA calibration overrides global
            # KALSHI_CALIB when PER_MARKET_CALIB_ENABLED + ≥5 samples in
            # market_calibration. Falls back to 0.25 prior on cold start.
            base_calib = kalshi_calib_for(ticker)

            # Offense O.2: hedge-eligible AND auto-hedge on → reduced adverse
            # cost. When AUTO_HEDGE_ENABLED is off we don't claim the hedge
            # exists yet (so we don't over-size).
            is_hedged_now = (
                getattr(settings, "AUTO_HEDGE_ENABLED", False)
                and _is_hedge_eligible(ticker)
            )

            y = MarketYield(
                market_id=ticker,
                pool_per_day=pool_d,
                our_size=optimal_size,
                top_book_size=int(comp),
                target_size=target,
                discount_factor=df,
                hours_to_settle=_hours_to_settle(end_date),
                midpoint=midpoint_default,
                calibration=base_calib * series_cal,
                series_priority=priority * setup_mult,
                observed_share=obs_share,
                is_hedged=is_hedged_now,
            )
            e_net_d = y.expected_daily_rebate
            yield_pct = y.yield_pct_daily
        except Exception as e:
            _log.debug(f"MarketYield failed for {ticker}: {e}")
            continue

        hours_left = _hours_to_settle(end_date)
        candidates.append({
            "market_ticker":           ticker,
            "series_ticker":           series or "",
            "reward_per_day_usd":      pool_d,
            "target_size":             target,
            "discount_factor":         df,
            "end_date":                end_date,
            "start_date":              "",
            "competition_observed":    round(comp, 1),
            "optimal_size_per_side":   optimal_size,
            "capital_per_market_usd":  round(capital, 2),
            "expected_net_per_day":    round(e_net_d, 4),
            "yield_pct_daily":         round(yield_pct * 100, 3),
            "series_priority":         priority,
            "confidence":              confidence,
            "observed_share":          obs_share,
            "hours_to_settle":         round(hours_left, 1),
            "is_daily":                hours_left <= DAILY_HOURS_THRESHOLD,
            "setup_score":             setup_mult,
            "setup_neighbors":         setup_score.get("neighbors", 0),
            "kelly_mult":              round(kelly_mult, 3),
            "pre_kelly_size":          pre_kelly_size,
        })

    # Sort by yield-per-dollar (most rebate per $ deployed)
    candidates.sort(key=lambda c: -c["yield_pct_daily"])

    # Daily-quota split: reserve a fraction of budget for fast-feedback markets
    daily_budget = budget_usd * DAILY_QUOTA_FRACTION
    nondaily_budget = budget_usd - daily_budget

    # Split into exploit (proven) and explore (blind) within each cadence
    daily_exploit = [c for c in candidates
                     if c["confidence"] != "blind_prior" and c["is_daily"]]
    daily_explore = [c for c in candidates
                     if c["confidence"] == "blind_prior" and c["is_daily"]]
    nondaily_exploit = [c for c in candidates
                        if c["confidence"] != "blind_prior" and not c["is_daily"]]
    nondaily_explore = [c for c in candidates
                        if c["confidence"] == "blind_prior" and not c["is_daily"]]

    selected: list[dict] = []
    daily_cap = 0.0
    nondaily_cap = 0.0

    def _fill(pool: list[dict], cap_used: float, cap_limit: float,
              attr_setter) -> float:
        """Greedy fill from pool until cap exhausted. Returns new cap_used."""
        for c in pool:
            if len(selected) >= MAX_PORTFOLIO_SIZE:
                break
            if cap_used + c["capital_per_market_usd"] <= cap_limit:
                selected.append(c)
                cap_used += c["capital_per_market_usd"]
        return cap_used

    # Fill DAILY pool first (exploit then explore)
    daily_cap = _fill(daily_exploit, daily_cap,
                      daily_budget * (1.0 - explore_fraction), None)
    daily_cap = _fill(daily_explore, daily_cap, daily_budget, None)

    # Then fill NON-DAILY pool (exploit then explore)
    nondaily_cap = _fill(nondaily_exploit, nondaily_cap,
                         nondaily_budget * (1.0 - explore_fraction), None)
    nondaily_cap = _fill(nondaily_explore, nondaily_cap, nondaily_budget, None)

    # Track exploit vs explore for reporting
    exploit_cap = sum(c["capital_per_market_usd"] for c in selected
                      if c["confidence"] != "blind_prior")
    explore_cap = sum(c["capital_per_market_usd"] for c in selected
                      if c["confidence"] == "blind_prior")

    # Floor: ensure at least MIN_PORTFOLIO_SIZE markets even if budget tight
    if len(selected) < MIN_PORTFOLIO_SIZE and len(candidates) >= MIN_PORTFOLIO_SIZE:
        selected = candidates[:MIN_PORTFOLIO_SIZE]
        exploit_cap = sum(c["capital_per_market_usd"] for c in selected
                          if c["confidence"] != "blind_prior")
        explore_cap = sum(c["capital_per_market_usd"] for c in selected
                          if c["confidence"] == "blind_prior")

    total_cap = exploit_cap + explore_cap
    total_pool = sum(c["reward_per_day_usd"] for c in selected)
    total_e_net = sum(c["expected_net_per_day"] for c in selected)
    n_exploit = sum(1 for c in selected if c["confidence"] != "blind_prior")
    n_explore = sum(1 for c in selected if c["confidence"] == "blind_prior")
    n_daily = sum(1 for c in selected if c["is_daily"])
    n_nondaily = len(selected) - n_daily
    _log.info(
        f"capital_allocator: {len(selected)}/{len(candidates)} markets "
        f"(daily={n_daily}@${daily_cap:.0f}, monthly={n_nondaily}@${nondaily_cap:.0f}, "
        f"exploit={n_exploit}, explore={n_explore}), "
        f"total=${total_cap:.0f}/${budget_usd:.0f}, pool=${total_pool:.0f}/d, "
        f"expected_net=${total_e_net:.2f}/d"
    )

    return selected


def report(db_path: str = settings.DB_PATH) -> None:
    """CLI: print current portfolio selection."""
    selected = select_optimal_portfolio(db_path=db_path)
    print(f"\n{'TICKER':<38} {'POOL/d':>7} {'COMP':>6} {'OPT_SZ':>7} "
          f"{'CAP$':>6} {'E_NET/d':>8} {'YIELD%/d':>9} {'CONF'}")
    print("─" * 110)
    cap = 0
    for m in selected:
        cap += m["capital_per_market_usd"]
        print(f"{m['market_ticker'][:36]:<38} ${m['reward_per_day_usd']:>5.0f} "
              f"{m['competition_observed']:>6.0f} {m['optimal_size_per_side']:>7} "
              f"${m['capital_per_market_usd']:>4.0f} ${m['expected_net_per_day']:>+6.3f} "
              f"{m['yield_pct_daily']:>8.2f}% {m['confidence']}")
    print(f"\n{len(selected)} markets, capital=${cap:.0f}, "
          f"E[net]=${sum(m['expected_net_per_day'] for m in selected):.2f}/d")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report()
