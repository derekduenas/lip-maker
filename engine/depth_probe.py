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


def has_exit_liquidity(
    yes_price_cents: float, yes_size: float,
    no_price_cents:  float, no_size:  float,
    *,
    min_exit_contracts:   int = 50,
    min_exit_price_cents: int = 5,
) -> dict:
    """Can we EXIT positions on this market if filled?

    LIP makers post BOTH yes and no bids. Whichever side fills, we hold a
    directional position. Exit path = buy the OPPOSITE side to neutralize
    (1 YES + 1 NO = $1 settled regardless of outcome).

    Diagnosed 2026-05-12: 38 stranded positions sitting at "no_bid_to_sell_into"
    — depth_probe approved entry on projected_share but the opposite-side
    bid vanished after we filled. Engine had no exit path → held to settle.

    Conservative gate: BOTH opposite-side bids must have ≥ min_exit_contracts
    at ≥ min_exit_price_cents (since either side could fill). Returns dict
    with verdict + diagnostic fields.
    """
    # If we fill on YES, exit by buying NO at no_price_cents (no_size available)
    yes_exit_ok = (no_size >= min_exit_contracts and
                   no_price_cents >= min_exit_price_cents)
    # If we fill on NO, exit by buying YES at yes_price_cents
    no_exit_ok  = (yes_size >= min_exit_contracts and
                   yes_price_cents >= min_exit_price_cents)
    can_exit = yes_exit_ok and no_exit_ok
    if can_exit:
        reason = "ok"
    elif not yes_exit_ok and not no_exit_ok:
        reason = (f"both_sides_thin: no={no_size:.0f}@{no_price_cents:.0f}c "
                  f"yes={yes_size:.0f}@{yes_price_cents:.0f}c")
    elif not yes_exit_ok:
        reason = (f"no_side_thin (can't exit a YES fill): "
                  f"no={no_size:.0f}@{no_price_cents:.0f}c "
                  f"min={min_exit_contracts}@{min_exit_price_cents}c")
    else:
        reason = (f"yes_side_thin (can't exit a NO fill): "
                  f"yes={yes_size:.0f}@{yes_price_cents:.0f}c "
                  f"min={min_exit_contracts}@{min_exit_price_cents}c")
    return {
        "can_exit":               can_exit,
        "yes_exit_ok":            yes_exit_ok,
        "no_exit_ok":             no_exit_ok,
        "opp_no_size":            no_size,
        "opp_no_price_cents":     no_price_cents,
        "opp_yes_size":           yes_size,
        "opp_yes_price_cents":    yes_price_cents,
        "reason":                 reason,
    }


def filter_by_depth(
    candidates: list[dict],
    client,
    min_share: float = 0.05,
    max_probes: int = 60,
    sleep_between: float = 0.05,
    *,
    exit_gate_enabled:    bool = True,
    min_exit_contracts:   int  = 50,
    min_exit_price_cents: int  = 5,
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

        # Share gate: pass if EITHER side meets min_share
        share_passes = (share_yes >= min_share) or (share_no >= min_share)

        # Exit-liquidity gate: BOTH opposite sides must have decent depth so
        # we can unwind whichever side fills. 2026-05-12 fix — previously
        # blind to thin-exit traps that stranded 38 positions.
        if exit_gate_enabled:
            ex = has_exit_liquidity(
                d["yes_price"], d["yes_size"],
                d["no_price"],  d["no_size"],
                min_exit_contracts=min_exit_contracts,
                min_exit_price_cents=min_exit_price_cents,
            )
            exit_passes = ex["can_exit"]
            exit_reason = ex["reason"]
        else:
            exit_passes = True
            exit_reason = "gate_disabled"

        passes = share_passes and exit_passes
        if not share_passes:
            verdict = "fail_share"
        elif not exit_passes:
            verdict = "fail_exit"
        else:
            verdict = "pass"

        c2 = {
            **c,
            "depth_share_yes":  round(share_yes, 4),
            "depth_share_no":   round(share_no, 4),
            "depth_yes_top":    d["yes_size"],
            "depth_no_top":     d["no_size"],
            "depth_yes_price":  d["yes_price"],
            "depth_no_price":   d["no_price"],
            "depth_gate":       verdict,
            "exit_reason":      exit_reason,
        }
        if passes:
            out.append(c2)
        else:
            rejected.append(c2)

    n_share_fail = sum(1 for r in rejected if r.get("depth_gate") == "fail_share")
    n_exit_fail  = sum(1 for r in rejected if r.get("depth_gate") == "fail_exit")
    if rejected:
        _log.info(
            f"depth_probe rejected {len(rejected)} markets "
            f"(probed {n_probed}, share_fail={n_share_fail}, exit_fail={n_exit_fail})"
        )
        for r in rejected[:5]:
            _log.info(
                f"  REJECT[{r['depth_gate']}] {r['market_ticker'][:35]} "
                f"size={r['optimal_size_per_side']} "
                f"yes_top={r['depth_yes_top']:.0f}@{r['depth_yes_price']:.0f}c "
                f"no_top={r['depth_no_top']:.0f}@{r['depth_no_price']:.0f}c "
                f"reason={r.get('exit_reason','-')[:60]}"
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
