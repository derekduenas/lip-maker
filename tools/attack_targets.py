"""ATTACK TARGETS — top markets ranked for max profit.

PURPOSE: Tell the AI Brain (and the human) EXACTLY where to attack right now.
Mirrors the Kalshi UI's "Liquidity Rewards" filter (highest rewards × competition
level × time-to-end), and adds:
  - `incentive_description` from the live API (e.g. "new_event" = freshly-launched
    program with low competition initially — high-priority hunt target)
  - `hours_remaining` urgency
  - effective_competitors estimate from observed share
  - attack_priority class (HIGH / MED / LOW)

Combines:
  /trade-api/v2/incentive_programs    — live reward data + incentive_description
  tools/competitor_density.scan()     — local share / qual / adverse stats
  market_blacklist                    — exclude blocked markets

Used by:
  - edge_hunter (Brain) state snapshot — auto-injected each cycle
  - Human via CLI: `python tools/attack_targets.py [--top 30] [--json]`
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_auth import KalshiClient

_log = logging.getLogger(__name__)

# Score boosts
NEW_EVENT_BOOST = 1.40           # incentive_description == "new_event"
LOW_COMP_BOOST_BELOW = 2         # if effective_competitors < 2 → boost
LOW_COMP_BOOST_MULT = 1.30
URGENT_HOURS_THRESHOLD = 12      # markets ending <12h get urgency boost
URGENT_BOOST = 1.20

# Attack priority thresholds (in $/day expected net)
HIGH_PRIORITY_NET_USD = 10.0
MED_PRIORITY_NET_USD = 2.0


def _fetch_live_programs(status: str = "active") -> list[dict]:
    """Pull fresh /incentive_programs (UNAUTH endpoint).
    Returns list of dicts with: market_ticker, period_reward, discount_factor_bps,
    target_size_fp, start_date, end_date, incentive_type, incentive_description,
    paid_out."""
    client = KalshiClient()
    out: list[dict] = []
    cursor = None
    seen = 0
    while True:
        try:
            params = {"status": status, "type": "liquidity", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = client.get_unauth("/incentive_programs", params=params)
        except Exception as e:
            _log.warning(f"_fetch_live_programs page failed: {e}")
            break
        out.extend(r.get("incentive_programs", []) or [])
        cursor = r.get("next_cursor")
        seen += 1
        if not cursor or seen > 50:    # safety cap
            break
    return out


def _hours_remaining(end_date_str: str | None) -> float | None:
    if not end_date_str:
        return None
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        delta = (end - datetime.now(timezone.utc)).total_seconds() / 3600
        return max(0.0, delta)
    except Exception:
        return None


def _is_blacklisted(conn: sqlite3.Connection, ticker: str) -> bool:
    try:
        r = conn.execute(
            "SELECT 1 FROM market_blacklist WHERE ticker=? AND datetime(expires_at) > datetime('now')",
            (ticker,),
        ).fetchone()
        return r is not None
    except Exception:
        return False


def _attack_priority(net_per_day: float, hours_remaining: float | None,
                     is_new_event: bool, eff_competitors: float | None) -> str:
    if net_per_day >= HIGH_PRIORITY_NET_USD:
        return "HIGH"
    if net_per_day >= MED_PRIORITY_NET_USD:
        return "MED"
    if is_new_event and net_per_day >= 0.5:
        return "MED"            # promote new events even at modest net
    return "LOW"


def compute_attack_targets(
    top_n: int = 30,
    db_path: str = settings.DB_PATH,
    include_low: bool = False,
    max_target_size: int = 2500,
) -> list[dict]:
    """Returns top-N ranked attack targets (highest projected net $/day first).

    Each row includes:
      market_ticker, series_ticker, reward_per_day_usd, observed_share,
      qual_rate, eff_competitors, hours_remaining, incentive_description,
      adverse_per_day, expected_net_per_day, attack_score, attack_priority,
      data_confidence, why
    """
    # 1. Live programs from API (gives us incentive_description, fresh start/end)
    live_programs = _fetch_live_programs(status="active")
    by_ticker_live: dict[str, dict] = {}
    for p in live_programs:
        t = p.get("market_ticker") or ""
        if t and not p.get("paid_out"):
            by_ticker_live[t] = p

    # 2. Local competitor_density (gives us share, qual, adverse)
    try:
        from tools.competitor_density import scan as _density_scan
        density_rows = _density_scan(db_path=db_path, max_target_size=max_target_size)
    except Exception as e:
        _log.warning(f"competitor_density failed: {e}")
        density_rows = []
    density_by_ticker = {d["market_ticker"]: d for d in density_rows if isinstance(d, dict)}

    # 3. Combine — iterate over union of live + density
    all_tickers = set(by_ticker_live.keys()) | set(density_by_ticker.keys())
    conn = sqlite3.connect(db_path, timeout=10.0)
    out: list[dict] = []
    try:
        for t in all_tickers:
            if _is_blacklisted(conn, t):
                continue
            live = by_ticker_live.get(t, {})
            d = density_by_ticker.get(t, {})

            reward_per_day = d.get("reward_per_day_usd", 0.0)
            if not reward_per_day and live:
                # Compute fresh from live program if missing locally
                period_reward_usd = float(live.get("period_reward", 0)) / 10_000.0
                try:
                    sd = datetime.fromisoformat(live["start_date"].replace("Z", "+00:00"))
                    ed = datetime.fromisoformat(live["end_date"].replace("Z", "+00:00"))
                    days = max(1.0, (ed - sd).total_seconds() / 86400)
                    reward_per_day = period_reward_usd / days
                except Exception:
                    reward_per_day = period_reward_usd

            if reward_per_day <= 0:
                continue

            share = d.get("observed_share")
            qual = d.get("qual_rate", 1.0)
            adverse = d.get("adverse_per_market_per_day", 0.0)
            n_snaps = d.get("n_snaps", 0)
            confidence = d.get("confidence", "prior")
            net_per_day = d.get("expected_net_per_day")
            if net_per_day is None:
                # No local data → conservative prior: 20% share, 100% qual, no adverse
                share = share or 0.20
                qual = qual or 1.0
                net_per_day = reward_per_day * share * qual

            # 2026-05-16: per-series calibration scale. Historical settlement
            # data shows weather series (KXHIGH*, KXLOW*) earn ~0% of pool
            # despite heavy quoting volume — they should not be picked. Pull
            # the EWMA from market_calibration and scale the projection. Series
            # with < 5 settlements fall back to the global 0.25 prior.
            try:
                from engine.calibration_ewma import calib_for
                series_pref = t.split("-", 1)[0] if "-" in t else t
                calib_scale = calib_for(series_pref, fallback=0.25)
                # Normalize: existing net_per_day already assumes ~25% capture in
                # its priors (share × qual ≈ 0.20). Multiply by (calib / 0.25)
                # to retune to learned rate. Result: 0.0001 calib → ~near-zero
                # net_per_day → automatically drops to LOW priority.
                calib_factor = max(calib_scale / 0.25, 0.0)
                net_per_day_raw = net_per_day
                net_per_day = net_per_day * calib_factor
            except Exception:
                calib_scale = None
                calib_factor = 1.0
                net_per_day_raw = net_per_day

            # Effective competitors estimate (1 = us only, higher = more)
            if share and share > 0:
                eff_comp = max(0.0, 1.0 / share - 1.0)
            else:
                eff_comp = None

            hours_remaining = _hours_remaining(live.get("end_date"))
            inc_desc = live.get("incentive_description", "")
            is_new_event = inc_desc == "new_event"
            target_size = float(live.get("target_size_fp") or d.get("target_size", 0))
            df_bps = live.get("discount_factor_bps")

            # Composite attack score
            score = max(0.01, net_per_day)
            why = []
            if is_new_event:
                score *= NEW_EVENT_BOOST
                why.append("new_event")
            if eff_comp is not None and eff_comp < LOW_COMP_BOOST_BELOW:
                score *= LOW_COMP_BOOST_MULT
                why.append(f"low_comp({eff_comp:.1f})")
            if hours_remaining is not None and hours_remaining < URGENT_HOURS_THRESHOLD:
                score *= URGENT_BOOST
                why.append(f"urgent({hours_remaining:.1f}h)")

            # Phase H (2026-05-18): hedge-aware boost. Markets with a verified
            # external hedge counterpart (HEDGE_MAP entry + kalshi_pm_manual_map
            # confirmation for PM-routed series) get a 1.3x score multiplier
            # because their adverse-selection cost is bounded by the basis
            # residual rather than realized P&L. Reasoning: with hedging the
            # rebate becomes near-pure income — we should prefer those markets.
            try:
                from cross_venue.market_match import hedge_for_ticker
                hedge_spec = hedge_for_ticker(t)
                if hedge_spec is not None:
                    # For PM venues, additionally require a manual_map entry
                    # (confirms PM actually has a counterpart for THIS ticker).
                    if hedge_spec.hedge_venue == "Polymarket":
                        try:
                            _c = sqlite3.connect(
                                f"file:{settings.DB_PATH}?mode=ro",
                                uri=True, timeout=1.0,
                            )
                            row = _c.execute(
                                "SELECT 1 FROM kalshi_pm_manual_map "
                                "WHERE kalshi_ticker = ? AND status='active'",
                                (t,),
                            ).fetchone()
                            _c.close()
                            if row:
                                score *= 1.30
                                why.append("hedged(PM-verified)")
                        except sqlite3.OperationalError:
                            pass   # manual map not populated yet
                    else:
                        # Kraken / CME / etc — series-level mapping is enough
                        score *= 1.30
                        why.append(f"hedged({hedge_spec.hedge_venue})")
            except Exception:
                pass

            priority = _attack_priority(net_per_day, hours_remaining, is_new_event, eff_comp)
            if not include_low and priority == "LOW":
                continue

            out.append({
                "market_ticker":         t,
                "series_ticker":         t.split("-", 1)[0] if "-" in t else t,
                "reward_per_day_usd":    round(reward_per_day, 3),
                "observed_share":        round(share, 4) if share is not None else None,
                "qual_rate":             round(qual, 3) if qual is not None else None,
                "eff_competitors":       round(eff_comp, 2) if eff_comp is not None else None,
                "n_snaps":               n_snaps,
                "data_confidence":       confidence,
                "hours_remaining":       round(hours_remaining, 1) if hours_remaining is not None else None,
                "incentive_description": inc_desc,
                "target_size":           int(target_size) if target_size else None,
                "discount_factor_bps":   df_bps,
                "adverse_per_day":       round(adverse, 3) if adverse else 0.0,
                "expected_net_per_day":  round(net_per_day, 3),
                "expected_net_pre_calib": round(net_per_day_raw, 3),
                "calibration":           round(calib_scale, 4) if calib_scale is not None else None,
                "attack_score":          round(score, 3),
                "attack_priority":       priority,
                "why":                   ",".join(why) if why else "",
            })
    finally:
        conn.close()

    out.sort(key=lambda x: x["attack_score"], reverse=True)
    return out[:top_n]


def brain_summary(top_n: int = 10) -> dict:
    """Compact JSON for injection into Brain prompt context."""
    targets = compute_attack_targets(top_n=top_n, include_low=False)
    n_high = sum(1 for t in targets if t["attack_priority"] == "HIGH")
    n_new_event = sum(1 for t in targets if t.get("incentive_description") == "new_event")
    return {
        "n_targets": len(targets),
        "n_high_priority": n_high,
        "n_new_event": n_new_event,
        "total_addressable_net_per_day_usd": round(sum(t["expected_net_per_day"] for t in targets), 2),
        "top_targets": [
            {
                "ticker":   t["market_ticker"],
                "priority": t["attack_priority"],
                "net_$/d":  t["expected_net_per_day"],
                "reward_$/d": t["reward_per_day_usd"],
                "share":    t["observed_share"],
                "comp":     t["eff_competitors"],
                "hrs_rem":  t["hours_remaining"],
                "why":      t["why"],
                "is_new":   t.get("incentive_description") == "new_event",
            }
            for t in targets
        ],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.add_argument("--include-low", action="store_true",
                   help="include LOW-priority targets in output")
    a = p.parse_args()
    targets = compute_attack_targets(top_n=a.top, include_low=a.include_low)
    if a.json:
        print(json.dumps(targets, default=str, indent=2))
    else:
        n_high = sum(1 for t in targets if t["attack_priority"] == "HIGH")
        n_new = sum(1 for t in targets if t.get("incentive_description") == "new_event")
        total_net = sum(t["expected_net_per_day"] for t in targets)
        print(f"\n=== ATTACK TARGETS — top {len(targets)} ===")
        print(f"   HIGH priority: {n_high}   NEW_EVENT: {n_new}   "
              f"Total addressable net: ${total_net:.2f}/day\n")
        print(f"{'#':<3} {'TICKER':<32} {'PRI':<5} {'NET$/d':>7} {'REW$/d':>7} "
              f"{'SHARE':>6} {'COMP':>5} {'HRS':>5} {'WHY':<25}")
        print("-" * 110)
        for i, t in enumerate(targets, 1):
            share_s = f"{t['observed_share']*100:.1f}%" if t['observed_share'] is not None else "  -"
            comp_s = f"{t['eff_competitors']:.1f}" if t['eff_competitors'] is not None else "  -"
            hrs_s = f"{t['hours_remaining']:.0f}" if t['hours_remaining'] is not None else "  -"
            print(f"{i:<3} {t['market_ticker'][:30]:<32} {t['attack_priority']:<5} "
                  f"{t['expected_net_per_day']:>7.2f} {t['reward_per_day_usd']:>7.2f} "
                  f"{share_s:>6} {comp_s:>5} {hrs_s:>5} {t['why'][:24]:<25}")
