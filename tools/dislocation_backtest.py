"""CLI runner for dislocation backtests — the LIVE_MODE gate.

Usage:
    python -m tools.dislocation_backtest                      # decomposition T-1/T-7/T-30
    python -m tools.dislocation_backtest --convergence        # spread-collapse backtest
    python -m tools.dislocation_backtest --json               # machine-readable
    python -m tools.dislocation_backtest --zq data/historical/zq_history.csv \
                                         --kalshi data/historical/kalshi_fed_history.csv

WHAT TO LOOK FOR:

  Decomposition pass criteria:
    T-1 hit rate ≥ 80%, mean abs error ≤ 5pp
    T-7 hit rate ≥ 65%, mean abs error ≤ 10pp
    T-30 mean abs error ≤ 20pp (less critical — directional only)

  Convergence pass criteria:
    convergence_rate ≥ 70% (% of pairs where final spread <5pp)
    mean_simulated_pnl ≥ 5pp (positive carry from spread closure)
    basis_blowups ≤ 5% of pairs (spec match holds)

  GRADUATION TO LIVE_MODE:
    Decomposition T-1 hit rate ≥ 80% on ≥30 historical FOMCs
    AND
    Convergence rate ≥ 70% on ≥10 paired-market histories
    OR equivalent dual-validation per operator judgment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dislocation.backtest.convergence import (
    load_kalshi_history,
    run_convergence_backtest,
)
from dislocation.backtest.decomposition import run_backtest
from dislocation.backtest.historical_fomc import HISTORICAL_FOMCS, fomcs_in_range
from dislocation.backtest.zq_history import ZQHistory
from dislocation.config import DATA_DIR, LOG_PATH

DEFAULT_ZQ_CSV     = Path(DATA_DIR) / "historical" / "zq_history.csv"
DEFAULT_KALSHI_CSV = Path(DATA_DIR) / "historical" / "kalshi_fed_history.csv"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zq",       type=Path, default=DEFAULT_ZQ_CSV,
                    help="Path to ZQ daily settlement history CSV.")
    ap.add_argument("--kalshi",   type=Path, default=DEFAULT_KALSHI_CSV,
                    help="Path to Kalshi rate-decision market history CSV.")
    ap.add_argument("--from-date", type=str, default=None, help="ISO date filter start")
    ap.add_argument("--to-date",   type=str, default=None, help="ISO date filter end")
    ap.add_argument("--lookbacks", type=str, default="1,7,30",
                    help="Comma-sep lookback days for decomposition (default 1,7,30).")
    ap.add_argument("--convergence", action="store_true",
                    help="Also run convergence backtest (requires Kalshi CSV).")
    ap.add_argument("--decomposition-only", action="store_true",
                    help="Only run decomposition (default if --convergence not set).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-meetings", action="store_true",
                    help="Print per-meeting results (verbose).")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    # ── Date filter ──────────────────────────────────────────────────
    start = dt.date.fromisoformat(args.from_date) if args.from_date else dt.date(2000, 1, 1)
    end   = dt.date.fromisoformat(args.to_date)   if args.to_date   else dt.date(2099, 12, 31)
    meetings = fomcs_in_range(start, end)

    # ── Load data ────────────────────────────────────────────────────
    zq = ZQHistory.load_csv(args.zq)
    if len(zq) == 0:
        print(f"WARNING: no ZQ history loaded from {args.zq}", file=sys.stderr)
        print(f"         drop a CSV at that path with format:", file=sys.stderr)
        print(f"         contract,date,settlement", file=sys.stderr)
        print(f"         ZQU24,2024-09-17,94.6850", file=sys.stderr)
        return 2

    # ── Decomposition ────────────────────────────────────────────────
    output: dict = {
        "ts": dt.datetime.utcnow().isoformat(),
        "meetings_in_range": len(meetings),
        "zq_settles_loaded": len(zq),
        "decomposition": {},
    }
    lookbacks = [int(x) for x in args.lookbacks.split(",") if x.strip()]
    for lb in lookbacks:
        results, stats = run_backtest(meetings, zq, lookback_days=lb)
        output["decomposition"][f"T-{lb}"] = stats.explain()
        if args.show_meetings:
            output["decomposition"][f"T-{lb}_meetings"] = [r.explain() for r in results]

    # ── Convergence (optional) ───────────────────────────────────────
    if args.convergence and not args.decomposition_only:
        kalshi = load_kalshi_history(args.kalshi)
        if not kalshi:
            print(f"WARNING: no Kalshi history at {args.kalshi}", file=sys.stderr)
            output["convergence"] = {"error": f"no Kalshi history loaded from {args.kalshi}"}
        else:
            cresults, cstats = run_convergence_backtest(kalshi, zq)
            output["convergence"] = cstats.explain()
            if args.show_meetings:
                output["convergence_pairs"] = [r.explain() for r in cresults]

    # ── Output ───────────────────────────────────────────────────────
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        _print_human(output)

    # ── Gate evaluation ──────────────────────────────────────────────
    return _evaluate_gate(output)


def _print_human(out: dict) -> None:
    print(f"\n=== Dislocation Backtest @ {out['ts']} ===")
    print(f"FOMCs in range: {out['meetings_in_range']}")
    print(f"ZQ settles loaded: {out['zq_settles_loaded']}")
    print()
    print("DECOMPOSITION:")
    for lb_label, stats in out["decomposition"].items():
        if lb_label.endswith("_meetings"):
            continue
        print(f"  {lb_label}: n={stats['n_meetings']:>2}  "
              f"hit_rate={stats['hit_rate_%']:>5.1f}%  "
              f"MAE={stats['mean_abs_err_pp']:>5.2f}pp  "
              f"brier={stats['brier']:.4f}")

    if "convergence" in out and "error" not in out["convergence"]:
        c = out["convergence"]
        print()
        print("CONVERGENCE:")
        print(f"  pairs={c['n_pairs']}  "
              f"converged={c['convergence_rate_%']:.1f}%  "
              f"mean_max_spread={c['mean_max_spread_pp']:.2f}pp  "
              f"mean_final={c['mean_final_pp']:.2f}pp")
        print(f"  mean simulated PnL: {c['mean_sim_pnl_%']:.2f}pp  basis_blowups: {c['basis_blowups']}")


def _evaluate_gate(out: dict) -> int:
    """Returns 0 if all live gates pass, 1 if validation fails, 2 if data missing."""
    print()
    print("─" * 50)
    decomp = out.get("decomposition", {})
    t1 = decomp.get("T-1") or decomp.get("T-1_meetings")
    if not t1 or "n_meetings" not in t1:
        print("⚠ insufficient data for decomposition gate")
        return 2

    pass_decomp = (t1["n_meetings"] >= 30 and
                   t1["hit_rate_%"] >= 80.0 and
                   t1["mean_abs_err_pp"] <= 5.0)

    if pass_decomp:
        print("✓ DECOMPOSITION GATE PASSED")
    else:
        print("✗ DECOMPOSITION GATE NOT YET MET")
        print(f"   need: T-1 n≥30, hit≥80%, MAE≤5pp")
        print(f"   have: T-1 n={t1['n_meetings']}, hit={t1['hit_rate_%']:.1f}%, MAE={t1['mean_abs_err_pp']:.2f}pp")

    if "convergence" in out and "n_pairs" in out["convergence"]:
        c = out["convergence"]
        pass_conv = (c["n_pairs"] >= 10 and
                     c["convergence_rate_%"] >= 70.0 and
                     c["basis_blowups"] <= max(1, c["n_pairs"] // 20))
        if pass_conv:
            print("✓ CONVERGENCE GATE PASSED")
        else:
            print("✗ CONVERGENCE GATE NOT YET MET")
            print(f"   need: pairs≥10, conv_rate≥70%, blowups≤5%")
            print(f"   have: pairs={c['n_pairs']}, conv={c['convergence_rate_%']:.1f}%, "
                  f"blowups={c['basis_blowups']}")
        return 0 if (pass_decomp and pass_conv) else 1
    else:
        print("ℹ run with --convergence after dropping kalshi_fed_history.csv")
        return 0 if pass_decomp else 1


if __name__ == "__main__":
    sys.exit(main())
