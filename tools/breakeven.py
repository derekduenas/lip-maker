#!/usr/bin/env python3
"""What would have to change for a quote to be both legal and profitable.

    python tools/breakeven.py --stream <stream.jsonl>

Two gates stand between us and a quote, and they fail for different
reasons. This reports the threshold for each rather than asserting that
"the venue is uneconomic".

  FEASIBILITY (risk/sentinel.py)  is the quote legal at all?
      A quote must be at least the size floor, and its gross must fit the
      per-market cap, which is a percentage of the SENTINEL's bankroll.
      Those two can be mutually unsatisfiable.

  ECONOMICS (engine/quote_economics.py)  does it beat not quoting?
      Reward against adverse selection, fees, exit cost and uncertainty.

Reported as thresholds: the bankroll at which a legal size first exists,
the reward share at which the economics break even, and the fee multiple
the current edge could absorb.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import constitution, settings

SIZE_FLOOR = 10          # risk/sentinel.py size_floor


def feasibility(yes_c: int, no_c: int, bankroll: float) -> dict:
    per_mkt_pct = constitution.MAX_PER_MARKET_PCT
    gross_pct = constitution.MAX_GROSS_EXPOSURE_PCT
    cap = bankroll * per_mkt_pct
    unit = (yes_c + no_c) / 100.0            # $ of gross per contract, both legs
    min_cost = SIZE_FLOOR * unit
    max_size = int(cap / unit) if unit > 0 else 0
    # Bankroll at which the floor first fits the per-market cap.
    need_bankroll = min_cost / per_mkt_pct if per_mkt_pct > 0 else float("inf")
    return {
        "yes_no_sum_cents": yes_c + no_c,
        "bankroll_usd": bankroll,
        "per_market_cap_usd": round(cap, 2),
        "gross_cap_usd": round(bankroll * gross_pct, 2),
        "size_floor": SIZE_FLOOR,
        "cost_of_floor_usd": round(min_cost, 2),
        "largest_legal_size": max_size,
        "feasible": max_size >= SIZE_FLOOR,
        "bankroll_needed_for_floor_usd": round(need_bankroll, 2),
        "shortfall_usd": round(max(0.0, need_bankroll - bankroll), 2),
    }


def economics_breakeven(e: dict) -> dict:
    """Thresholds implied by one Economics record."""
    rew = float(e.get("expected_reward_usd", 0.0))
    costs = (-float(e.get("expected_trading_pnl_usd", 0.0))
             + float(e.get("expected_fees_usd", 0.0))
             + float(e.get("expected_exit_cost_usd", 0.0))
             + float(e.get("operating_cost_usd", 0.0))
             + float(e.get("uncertainty_allowance_usd", 0.0)))
    net = float(e.get("net_usd", rew - costs))
    share = float(e.get("assumptions", {})
                  .get("qualification", {}).get("our_share", 0.0) or 0.0)
    fees = float(e.get("expected_fees_usd", 0.0)) + float(
        e.get("expected_exit_cost_usd", 0.0))
    out = {
        "candidate": e.get("candidate"),
        "net_usd": round(net, 6),
        "reward_usd": round(rew, 6),
        "total_costs_usd": round(costs, 6),
        "our_share": share,
    }
    # Reward share that would make net zero, holding costs fixed.
    if rew > 0 and share > 0:
        out["breakeven_share"] = round(share * costs / rew, 10)
        out["share_multiple_needed"] = round(costs / rew, 4)
    # How much more fee the current edge could absorb.
    if net > 0 and fees > 0:
        out["fee_multiple_absorbable"] = round((fees + net) / fees, 3)
    elif fees > 0:
        out["fee_multiple_absorbable"] = 0.0
    # How much of the modelled edge survives losing half the fills.
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", required=True)
    ap.add_argument("--experiment", default="")
    a = ap.parse_args()

    bank = float(getattr(settings, "BANKROLL_USD", 80))
    ledger = float(getattr(settings, "ACCOUNT_OPENING_CASH_USD", 5000.0))

    latest = {}
    for line in Path(a.stream).read_text().splitlines():
        if not line:
            continue
        ev = json.loads(line)
        if ev["kind"] == "book":
            latest[ev["ticker"]] = ev

    print("=" * 74)
    print("BREAK-EVEN THRESHOLDS")
    print("=" * 74)
    print(f"sentinel bankroll  ${bank:,.2f}   (settings.BANKROLL_USD, "
          f"{'ABSENT -> default 80' if not hasattr(settings,'BANKROLL_USD') else 'set'})")
    print(f"account ledger     ${ledger:,.2f}   (the shared account the "
          f"experiment funds)")
    print(f"per-market cap     {constitution.MAX_PER_MARKET_PCT:.0%} of bankroll")
    print(f"size floor         {SIZE_FLOOR} contracts\n")

    print("FEASIBILITY per captured market")
    hdr = (f"{'market':40} {'y+n':>4} {'cap$':>7} {'floor$':>7} "
           f"{'maxsz':>6} {'ok':>3} {'need$':>8}")
    print(hdr); print("-" * len(hdr))
    worst = 0.0
    for t, ev in latest.items():
        if not ev["yes"] or not ev["no"]:
            continue
        y = max(p for p, _ in ev["yes"]); n = max(p for p, _ in ev["no"])
        f = feasibility(y, n, bank)
        worst = max(worst, f["bankroll_needed_for_floor_usd"])
        print(f"{t:40} {f['yes_no_sum_cents']:4d} "
              f"{f['per_market_cap_usd']:7.2f} {f['cost_of_floor_usd']:7.2f} "
              f"{f['largest_legal_size']:6d} {'yes' if f['feasible'] else 'no':>3} "
              f"{f['bankroll_needed_for_floor_usd']:8.2f}")
    print(f"\n  -> the sentinel bankroll must reach ${worst:,.2f} before ANY "
          f"legal quote exists\n     on the richest of these books; it is "
          f"${bank:,.2f}, short by ${max(0.0, worst-bank):,.2f}.")
    print(f"  -> at the ledger's ${ledger:,.2f} the per-market cap would be "
          f"${ledger*constitution.MAX_PER_MARKET_PCT:,.2f}, admitting "
          f"~{int(ledger*constitution.MAX_PER_MARKET_PCT/0.99)} contracts.")
    print("\n  Reconciling the two is a RISK decision and is not made here.")

    if a.experiment:
        rep = json.loads(Path(a.experiment).read_text())
        arm = rep["arms"].get("fixed_60s", {})
        print("\nECONOMICS break-even, best candidate per market")
        for t, v in (arm.get("candidates_considered") or {}).items():
            cons = [c for c in ((v.get("economics") or {}).get("considered") or [])
                    if c.get("candidate") != "no_quote"]
            if not cons:
                continue
            best = max(cons, key=lambda c: c["net_usd"])
            b = economics_breakeven(best)
            print(f"  {t:38} {str(b['candidate']):>12} net ${b['net_usd']:>9.4f} "
                  f"share {b['our_share']:.3e} "
                  f"needs x{b.get('share_multiple_needed', float('nan')):.2f} share "
                  f"| absorbs x{b.get('fee_multiple_absorbable', 0):.2f} fees")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
