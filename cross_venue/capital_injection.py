#!/usr/bin/env python3
"""DCA Capital Injection Allocator.

When biweekly $800 DCA fires, this script reads the orchestrator's RYD
history and recommends how to split between Kalshi and PM.

Logic (NEXUS Ship 3):
  1. Pull last 14d of RYD readings from /root/logs/orchestrator.db
  2. Compute trailing 7d mean RYD per venue (filters noise)
  3. Allocate proportional to positive RYD; halt deposits to negative-RYD venue
  4. Output a deposit recommendation, log it, optionally execute (v2)

USAGE:
  python capital_injection.py                    # default $800 DCA
  python capital_injection.py --amount 1600      # custom amount
  python capital_injection.py --json
  python capital_injection.py --execute          # v2 — NOT IMPLEMENTED
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

ORCH_DB = "/root/logs/orchestrator.db"
DEFAULT_DCA_USD = 800.0
LOOKBACK_DAYS = 7
MIN_RYD_TO_FUND = 0.0001  # 0.01%/d — below this, halt that venue


def trailing_ryd(venue: str, days: int = LOOKBACK_DAYS) -> dict:
    """Mean RYD + sample count from orchestrator history."""
    if not os.path.exists(ORCH_DB):
        return {"venue": venue, "samples": 0, "mean_ryd": None, "error": "no orchestrator history"}
    conn = sqlite3.connect(ORCH_DB, timeout=5.0)
    try:
        col = "kalshi_ryd" if venue == "kalshi" else "pm_ryd"
        cap = "kalshi_capital" if venue == "kalshi" else "pm_capital"
        rows = conn.execute(f"""
            SELECT {col}, {cap}
            FROM ryd_history
            WHERE datetime(snapshot_at) > datetime('now','-{days} days')
              AND {col} IS NOT NULL
        """).fetchall()
        ryds = [r[0] for r in rows if r[0] is not None]
        caps = [r[1] for r in rows if r[1] is not None]
    finally:
        conn.close()
    if not ryds:
        return {"venue": venue, "samples": 0, "mean_ryd": None}
    return {
        "venue":     venue,
        "samples":   len(ryds),
        "mean_ryd":  sum(ryds) / len(ryds),
        "max_ryd":   max(ryds),
        "min_ryd":   min(ryds),
        "mean_cap":  sum(caps) / len(caps) if caps else 0,
    }


def allocate(amount: float, k: dict, p: dict) -> dict:
    """Recommend split based on trailing RYD."""
    out = {"amount": amount, "kalshi": 0, "pm": 0, "reasoning": ""}
    k_ryd = k.get("mean_ryd")
    p_ryd = p.get("mean_ryd")
    k_n = k.get("samples", 0)
    p_n = p.get("samples", 0)

    # Edge cases
    if k_n < 5 and p_n < 5:
        # No data on either — split 50/50
        out["kalshi"] = round(amount * 0.5, 2)
        out["pm"]     = round(amount * 0.5, 2)
        out["reasoning"] = "insufficient RYD history both venues — default 50/50"
        return out

    if k_ryd is None or k_n < 5:
        out["pm"] = amount
        out["reasoning"] = "no Kalshi RYD data — full to PM"
        return out
    if p_ryd is None or p_n < 5:
        out["kalshi"] = amount
        out["reasoning"] = "no PM RYD data — full to Kalshi"
        return out

    # Both have data. Halt-deposit on negative RYD.
    k_pos = max(0.0, k_ryd)
    p_pos = max(0.0, p_ryd)
    if k_pos == 0 and p_pos == 0:
        out["kalshi"] = round(amount * 0.5, 2)
        out["pm"]     = round(amount * 0.5, 2)
        out["reasoning"] = f"both RYDs negative or zero — default 50/50 (k={k_ryd:+.4%}/d, p={p_ryd:+.4%}/d)"
        return out

    # Allocate proportional to positive RYD
    total = k_pos + p_pos
    k_share = k_pos / total
    p_share = p_pos / total
    out["kalshi"] = round(amount * k_share, 2)
    out["pm"]     = round(amount * p_share, 2)
    out["reasoning"] = (f"proportional to RYD: kalshi={k_ryd:+.4%}/d ({k_share*100:.0f}%), "
                        f"pm={p_ryd:+.4%}/d ({p_share*100:.0f}%)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", type=float, default=DEFAULT_DCA_USD)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()

    if a.execute:
        print("🚧 --execute not implemented (v2). Manual deposits required.", file=sys.stderr)
        return 2

    k = trailing_ryd("kalshi")
    p = trailing_ryd("pm")
    rec = allocate(a.amount, k, p)

    if a.json:
        print(json.dumps({
            "ts":             datetime.now(timezone.utc).isoformat(),
            "kalshi_history": k,
            "pm_history":     p,
            "recommendation": rec,
        }, indent=2, default=str))
    else:
        print(f"\n💸 DCA ALLOCATION — ${a.amount:.0f} biweekly")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        ryd_k = k.get('mean_ryd')
        ryd_p = p.get('mean_ryd')
        k_str = f"{ryd_k*100:+.4f}%/d" if ryd_k is not None else "n/a"
        p_str = f"{ryd_p*100:+.4f}%/d" if ryd_p is not None else "n/a"
        print(f"  Kalshi 7d RYD:  {k_str:>14}  ({k.get('samples',0)} samples)")
        print(f"  PM     7d RYD:  {p_str:>14}  ({p.get('samples',0)} samples)")
        print(f"")
        print(f"  → Send ${rec['kalshi']:>7.2f} to Kalshi")
        print(f"  → Send ${rec['pm']:>7.2f} to PM")
        print(f"")
        print(f"  Why: {rec['reasoning']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
