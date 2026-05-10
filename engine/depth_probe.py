"""Pre-deployment depth probe — auditor's #1 fix.

LIP rewards are pro-rata: our_share = our_size / (top_depth + our_size).
Without this gate, we deploy capital into markets where 4000+ contracts
are stacked at best price → our 30-contract quote earns <1% share.

Probe each candidate market's orderbook BEFORE deployment. Reject if
projected share at best price < threshold (default 5%).

Usage:
    from engine.depth_probe import filter_by_depth
    candidates = select_optimal_portfolio(...)
    candidates = filter_by_depth(candidates, kalshi_client, min_share=0.05)
"""
from __future__ import annotations
import logging
import time

_log = logging.getLogger(__name__)


def _parse_orderbook(raw: dict) -> tuple[float, float, float, float]:
    """Returns (best_yes_price, best_yes_size, best_no_price, best_no_size).
    Prices in cents (0-100), sizes in contracts. Returns (0,0,0,0) on parse fail.
    """
    ob = raw.get("orderbook_fp") or raw.get("orderbook") or {}
    yes = ob.get("yes_dollars") or ob.get("yes") or []
    no = ob.get("no_dollars") or ob.get("no") or []

    def best(book):
        if not book:
            return 0.0, 0.0
        try:
            top = max(book, key=lambda x: float(x[0]))
            # yes_dollars: price as "0.0100", size as "12.00" (contract count)
            return float(top[0]) * 100, float(top[1])
        except (ValueError, TypeError, IndexError):
            return 0.0, 0.0

    yp, ys = best(yes)
    np_, ns = best(no)
    return yp, ys, np_, ns


def projected_share(our_size: float, top_depth: float) -> float:
    """Pro-rata share if we add `our_size` to a level with `top_depth`."""
    if our_size <= 0:
        return 0.0
    return our_size / max(0.001, (top_depth + our_size))


def probe(client, ticker: str) -> dict:
    """Single-market depth probe. Returns parsed top-of-book."""
    try:
        r = client.get(f"/markets/{ticker}/orderbook")
        yp, ys, np_, ns = _parse_orderbook(r)
        return {
            "ok": True, "yes_price": yp, "yes_size": ys,
            "no_price": np_, "no_size": ns,
        }
    except Exception as e:
        return {"ok": False, "error": str(e),
                "yes_price": 0, "yes_size": 0, "no_price": 0, "no_size": 0}


def filter_by_depth(
    candidates: list[dict],
    client,
    min_share: float = 0.05,
    max_probes: int = 60,
    sleep_between: float = 0.05,
) -> list[dict]:
    """Reject candidates whose projected share < min_share on EITHER side.

    Adds depth_share_yes, depth_share_no, depth_yes_top, depth_no_top to
    each surviving candidate dict. Limits probes to max_probes (top
    candidates by yield_pct_daily — already sorted by capital_allocator).

    Empty book on a side = automatic pass for that side (we'd be alone).
    """
    out = []
    rejected = []
    n_probed = 0
    for c in candidates:
        if n_probed >= max_probes:
            # past probe budget — keep remaining without gating (conservative)
            out.append({**c, "depth_gate": "unprobed"})
            continue

        ticker = c["market_ticker"]
        our_size = c["optimal_size_per_side"]
        d = probe(client, ticker)
        n_probed += 1
        time.sleep(sleep_between)

        if not d["ok"]:
            # API error — keep candidate (don't penalize for our network)
            out.append({**c, "depth_gate": "probe_error"})
            continue

        # Projected share if we joined at best price on each side
        share_yes = projected_share(our_size, d["yes_size"])
        share_no = projected_share(our_size, d["no_size"])

        # Pass if EITHER side meets min_share (we can quote one side only)
        # Empty book → share_X = 1.0 (we're alone), passes trivially
        passes = (share_yes >= min_share) or (share_no >= min_share)

        c2 = {
            **c,
            "depth_share_yes": round(share_yes, 4),
            "depth_share_no": round(share_no, 4),
            "depth_yes_top": d["yes_size"],
            "depth_no_top": d["no_size"],
            "depth_gate": "pass" if passes else "fail",
        }
        if passes:
            out.append(c2)
        else:
            rejected.append(c2)

    if rejected:
        _log.info(f"depth_probe rejected {len(rejected)} markets (probed {n_probed})")
        for r in rejected[:5]:
            _log.info(
                f"  REJECT {r['market_ticker'][:35]} "
                f"size={r['optimal_size_per_side']} "
                f"yes_top={r['depth_yes_top']:.0f} ({r['depth_share_yes']*100:.1f}%) "
                f"no_top={r['depth_no_top']:.0f} ({r['depth_share_no']*100:.1f}%)"
            )

    return out


# Self-test / audit harness
if __name__ == "__main__":
    import sys, json
    sys.path.insert(0, "/root/lip-maker")
    from execution.kalshi_auth import KalshiClient
    from engine.capital_allocator import select_optimal_portfolio

    client = KalshiClient()
    candidates = select_optimal_portfolio(budget_usd=1028)
    print(f"\nPre-gate: {len(candidates)} candidates")
    print(f"\n{'ticker':<38} {'size':>4} {'yes_top':>8} {'sh_y':>6} {'no_top':>8} {'sh_n':>6} {'gate':>6}")
    print("-" * 90)

    filtered = filter_by_depth(candidates, client, min_share=0.05)
    for c in candidates[:30]:
        # find in filtered (may be missing if rejected)
        f = next((x for x in filtered if x["market_ticker"] == c["market_ticker"]), None)
        if f and "depth_share_yes" in f:
            gate = f.get("depth_gate", "?")
            print(f"{c['market_ticker'][:38]:<38} {c['optimal_size_per_side']:>4} "
                  f"{f['depth_yes_top']:>8.0f} {f['depth_share_yes']*100:>5.1f}% "
                  f"{f['depth_no_top']:>8.0f} {f['depth_share_no']*100:>5.1f}% {gate:>6}")
        elif f:
            print(f"{c['market_ticker'][:38]:<38} {c['optimal_size_per_side']:>4} "
                  f"{'unprobed':>30}")
        else:
            print(f"{c['market_ticker'][:38]:<38} {c['optimal_size_per_side']:>4} "
                  f"{'REJECTED':>30}")

    print(f"\nPre-gate:  {len(candidates)} candidates")
    print(f"Post-gate: {len(filtered)} candidates")
    rejected = len(candidates) - len(filtered)
    pct = 100 * rejected / max(1, len(candidates))
    print(f"Rejected:  {rejected} ({pct:.0f}%)")
