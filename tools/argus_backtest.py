"""ARGUS backtest CLI — pull settled markets, train brain, score with BSS."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.kalshi_auth import KalshiClient
from argus.backtest.runner import run_backtest
from argus.config import BSS_LIVE_GATE, MIN_BACKTEST_N
from argus.data.spotify_charts import SpotifyChartsClient


SERIES_BY_BRAIN = {
    "music": "KXRANKLISTSONGSPOTGLOBAL",
}


def pull_settled(series_ticker: str, max_pages: int = 20) -> list[dict]:
    c = KalshiClient()
    out: list[dict] = []
    cursor = None
    pages = 0
    while pages < max_pages:
        params = {"series_ticker": series_ticker, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = c.get_unauth("/markets", params=params)
        ms = r.get("markets", [])
        out.extend(ms)
        pages += 1
        cursor = r.get("cursor")
        if not cursor or not ms:
            break
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", required=True, choices=list(SERIES_BY_BRAIN.keys()))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    series = SERIES_BY_BRAIN[a.brain]
    settled = pull_settled(series)
    print(f"\n[ARGUS BACKTEST] brain={a.brain}  pulled {len(settled)} settled markets\n")

    client = SpotifyChartsClient()
    res = run_backtest(settled, client=client)

    if a.json:
        print(json.dumps(res.explain(), indent=2))
        return 0

    # Human report
    print(f"━━━ ARGUS BACKTEST — {a.brain} ━━━")
    print(f"  method:           {res.method}")
    print(f"  n_total:          {res.n_total}")
    print(f"  n_train_per_fold: {res.n_train}")
    print(f"  n_test:           {res.n_test}")
    print(f"  base_rate:        {res.base_rate:.4f}")
    print(f"  naive_brier:      {res.naive_brier:.4f}  (predicting base_rate for all)")
    if res.train_brier:
        print(f"  TRAIN brier:      {res.train_brier.brier:.4f}  "
              f"skill={res.train_brier.skill:+.4f}")
    if res.test_brier:
        print(f"  TEST  brier:      {res.test_brier.brier:.4f}  "
              f"skill={res.test_brier.skill:+.4f}")
    print(f"  TEST BSS:         {res.test_bss:+.4f}  (gate >= {BSS_LIVE_GATE})")
    print(f"  PASSES GATE:      "
          f"{'✓ YES' if res.passes_gate(BSS_LIVE_GATE, MIN_BACKTEST_N) else '✗ NO'}")
    print()
    print("  feature importance (logistic weights):")
    for k, v in res.feature_importance.items():
        marker = "  ←" if abs(v) >= 1.0 else ""
        print(f"    {k:<35} {v:+.4f}{marker}")
    print()
    print("  calibration (test preds → realized freq):")
    print(f"    {'bin':<14} {'n':>4} {'pred_avg':>10} {'real_freq':>10}")
    for b in res.calibration:
        if b["n"] == 0:
            continue
        print(f"    [{b['bin_lo']:.1f}, {b['bin_hi']:.1f})       "
              f"{b['n']:>4} {b['predicted_avg']:>10} {b['realized_freq']:>10}")
    print()
    if res.notes:
        print("  notes:")
        for n in res.notes:
            print(f"    - {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
