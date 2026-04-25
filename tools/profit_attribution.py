"""Per-market profit attribution — NET P&L decomposition.

For every market we've touched since live-flip, compute:
  net_pnl = rebate_earned_est + principal_pnl - fees

Where:
  rebate_earned_est = avg(share when valid) × reward × hours_active / 24
                     (from lip_snapshots × lip_programs)
  principal_pnl     = settlement_log.our_realized_usd (settled)
                     OR MTM from current Kalshi mid (open)  — deferred to v2
  fees              = 0 (Kalshi maker is free)

Classification (settled markets only):
  HERO      = net ≥ $5 AND ratio ≥ 3.0
  PROFIT    = net ≥ $0
  BREAKEVEN = -$2 < net < $2
  LOSER     = net ≤ -$2

Output: ranked table + aggregate stats. Feeds P0-3 auto-prune (week 2).
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

LIVE_FLIP_UTC = "2026-04-21T01:48:00"


def compute_per_market(db_path: str = settings.DB_PATH) -> list[dict]:
    """For each market with fills since live-flip, decompose P&L."""
    conn = sqlite3.connect(db_path)
    try:
        # Set of markets where we had fills
        tickers = [r[0] for r in conn.execute(
            """SELECT DISTINCT ticker FROM fill_ledger
               WHERE created_at >= ?""",
            (LIVE_FLIP_UTC,),
        ).fetchall()]

        results = []
        for tkr in tickers:
            # Principal P&L — from settlement_log (only if settled)
            s = conn.execute(
                """SELECT kalshi_result, our_realized_usd, our_position_yes, our_position_no
                   FROM settlement_log WHERE ticker = ?""",
                (tkr,),
            ).fetchone()
            settled = s is not None
            principal_pnl = s[1] if settled else None
            yes_n = s[2] if settled else 0
            no_n  = s[3] if settled else 0

            # Rebate accrual estimate from snapshots
            r = conn.execute(
                """SELECT COUNT(*) AS n_snaps,
                          AVG(CASE WHEN snapshot_valid=1 THEN our_score END) / 2.0 AS share,
                          MIN(captured_at) AS first,
                          MAX(captured_at) AS last,
                          p.reward_per_day_usd
                   FROM lip_snapshots s
                   LEFT JOIN lip_programs p ON s.market_ticker = p.market_ticker
                   WHERE s.market_ticker = ?
                     AND datetime(s.captured_at) >= datetime(?)
                   GROUP BY s.market_ticker""",
                (tkr, LIVE_FLIP_UTC),
            ).fetchone()

            rebate_est = 0.0
            hours_active = 0.0
            share_pct = 0.0
            if r and r[4]:
                n_snaps, share, first, last, reward = r
                share = share or 0
                share_pct = share * 100
                if first and last:
                    dt_first = datetime.fromisoformat(first)
                    dt_last  = datetime.fromisoformat(last)
                    hours_active = (dt_last - dt_first).total_seconds() / 3600
                    rebate_est = share * reward * (hours_active / 24)

            fees = 0.0
            # Classification based on REALIZED principal P&L only.
            # Rebate is ESTIMATED until Apr 28 first payout validates the formula.
            # Projected net shown separately so we see what we expect to keep.
            if settled:
                principal = principal_pnl or 0
                # Projected net (principal + estimated rebate) — for reference only
                projected_net = principal + rebate_est - fees
                # Ratio: how many rebate-dollars per dollar of realized loss?
                if principal < -0.01:
                    ratio = rebate_est / abs(principal)
                else:
                    ratio = None
                # Classification on PRINCIPAL, not projected:
                if principal >= 2:
                    cls = "HERO"       # principal profit — rebate is bonus
                elif principal >= -0.50:
                    cls = "FLAT"       # near-breakeven on principal
                elif ratio is not None and ratio >= 3.0:
                    cls = "COVERED"    # principal loss but rebate covers 3x+
                else:
                    cls = "LOSER"      # principal loss NOT covered by est rebate
                net = projected_net
            else:
                net = None  # unrealized
                ratio = None
                cls = "OPEN"

            # Fills summary
            fills = conn.execute(
                """SELECT SUM(CASE WHEN side='yes' THEN count ELSE 0 END),
                          SUM(CASE WHEN side='no'  THEN count ELSE 0 END)
                   FROM fill_ledger
                   WHERE ticker = ? AND created_at >= ?""",
                (tkr, LIVE_FLIP_UTC),
            ).fetchone()
            fills_yes = int(fills[0] or 0)
            fills_no  = int(fills[1] or 0)
            total_f = fills_yes + fills_no
            imbal = abs(fills_yes - fills_no) / total_f if total_f > 0 else 0

            results.append({
                "ticker":        tkr,
                "settled":       settled,
                "classification": cls,
                "principal_pnl": round(principal_pnl, 2) if principal_pnl is not None else None,
                "rebate_est":    round(rebate_est, 2),
                "net_pnl":       round(net, 2) if net is not None else None,
                "ratio":         round(ratio, 2) if ratio is not None else None,
                "share_pct":     round(share_pct, 1),
                "hours_active":  round(hours_active, 1),
                "fills_yes":     fills_yes,
                "fills_no":      fills_no,
                "fill_imbal":    round(imbal, 2),
            })

        return results
    finally:
        conn.close()


def summary(db_path: str = settings.DB_PATH) -> dict:
    markets = compute_per_market(db_path=db_path)

    # Aggregate
    heroes    = [m for m in markets if m["classification"] == "HERO"]
    flat      = [m for m in markets if m["classification"] == "FLAT"]
    covered   = [m for m in markets if m["classification"] == "COVERED"]
    losers    = [m for m in markets if m["classification"] == "LOSER"]
    open_mkts = [m for m in markets if m["classification"] == "OPEN"]

    settled = [m for m in markets if m["settled"]]
    total_settled_net = sum(m["net_pnl"] or 0 for m in settled)
    total_settled_principal = sum(m["principal_pnl"] or 0 for m in settled)
    total_settled_rebate = sum(m["rebate_est"] for m in settled)
    total_open_rebate = sum(m["rebate_est"] for m in open_mkts)

    # Aggregate ratio: rebate / |total_loss|
    total_losses = sum(abs(m["principal_pnl"]) for m in losers if m["principal_pnl"] is not None)
    aggregate_ratio = total_settled_rebate / total_losses if total_losses > 0.01 else None

    return {
        "markets_total":         len(markets),
        "hero_count":            len(heroes),
        "flat_count":            len(flat),
        "covered_count":         len(covered),
        "loser_count":           len(losers),
        "open_count":            len(open_mkts),
        "settled_projected_net": round(total_settled_net, 2),
        "settled_principal_real": round(total_settled_principal, 2),
        "settled_rebate_est":    round(total_settled_rebate, 2),
        "open_rebate_accrued":   round(total_open_rebate, 2),
        "aggregate_ratio":       round(aggregate_ratio, 2) if aggregate_ratio else None,
        "heroes":                sorted(heroes, key=lambda x: -(x["principal_pnl"] or 0)),
        "losers":                sorted(losers, key=lambda x: (x["principal_pnl"] or 0)),
        "covered":               sorted(covered, key=lambda x: (x["principal_pnl"] or 0)),
        "open_markets":          sorted(open_mkts, key=lambda x: -x["rebate_est"]),
        "all":                   markets,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=10)
    a = p.parse_args()

    s = summary()
    print(f"\n══ PROFIT ATTRIBUTION ══")
    print(f"  markets touched since live-flip: {s['markets_total']}")
    print(f"  settled: 🏆 {s['hero_count']}hero / ⚖ {s['flat_count']}flat / 🛡 {s['covered_count']}covered / 🔴 {s['loser_count']}LOSER")
    print(f"  still open: {s['open_count']}")
    print()
    print(f"  REAL cash so far:       ${s['settled_principal_real']:+.2f}   (principal P&L realized at settle)")
    print(f"  ESTIMATED rebate:       ${s['settled_rebate_est']:+.2f}   (from share×reward×hours; Apr 28 validates)")
    print(f"  projected settled net:  ${s['settled_projected_net']:+.2f}   (real + estimated, NOT yet received)")
    print(f"  open-market rebate est: ${s['open_rebate_accrued']:+.2f}   (still accruing)")
    if s['aggregate_ratio']:
        flag = "✅" if s['aggregate_ratio'] >= 3.0 else "🟡" if s['aggregate_ratio'] >= 1.5 else "🔴"
        print(f"  rebate/|loss| ratio:    {s['aggregate_ratio']}x  {flag}  (target ≥ 3.0 for elite profitability)")

    if s['heroes']:
        print(f"\n  🏆 HEROES — principal profit alone (top {a.top}):")
        for m in s['heroes'][:a.top]:
            print(f"    {m['ticker'][:36]:<38s}  prin=${m['principal_pnl']:+5.2f}  "
                  f"rebate_est=${m['rebate_est']:+5.2f}  share={m['share_pct']:.1f}%")

    if s['covered']:
        print(f"\n  🛡  COVERED — principal loss, rebate ratio ≥ 3x (top {a.top}):")
        for m in s['covered'][:a.top]:
            r_str = f"{m['ratio']:.1f}x" if m['ratio'] is not None else "-"
            print(f"    {m['ticker'][:36]:<38s}  prin=${m['principal_pnl']:+5.2f}  "
                  f"rebate_est=${m['rebate_est']:+5.2f}  ratio={r_str}")

    if s['losers']:
        print(f"\n  🔴 LOSERS — principal loss NOT covered by est rebate (top {a.top}):")
        for m in s['losers'][:a.top]:
            r_str = f"{m['ratio']:.1f}x" if m['ratio'] is not None else "-"
            print(f"    {m['ticker'][:36]:<38s}  prin=${m['principal_pnl']:+5.2f}  "
                  f"rebate_est=${m['rebate_est']:+5.2f}  ratio={r_str}  imbal={int(m['fill_imbal']*100)}%")


if __name__ == "__main__":
    main()
