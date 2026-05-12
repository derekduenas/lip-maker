"""ARGUS validation dashboard — one-line view of paper-book health.

Per (brain_id, chart_region) bucket:
  open:     count, $deployed, oldest position age
  resolved: count, cumulative paper_pnl, win-rate, Brier, BSS vs naive

BSS (Brier Skill Score) = 1 - (model_brier / naive_brier), where the
naive baseline predicts the per-bucket realized YES rate for every market.
BSS > 0 means the model beats naive; <= 0 means it doesn't. Drift alert
fires when BSS drops below 0.10 (after at least 10 resolutions — too noisy
before that).

Cron: deploy/argus-status.{service,timer} → daily 14:00 UTC, right after
the 13:20 argus-scan run + reconciler.

USAGE:
  python -m tools.argus_status            # human view
  python -m tools.argus_status --json     # machine-readable
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.config import DATA_DIR


CANDIDATES_DB = lambda: Path(DATA_DIR) / "argus_candidates.db"
PAPER_BANKROLL = float(
    os.getenv("ARGUS_PAPER_BANKROLL")
    or os.getenv("ARGUS_BANKROLL", "1000")
)
BSS_DRIFT_THRESHOLD = 0.10
BSS_MIN_N = 10            # need ≥ this many resolved to assess drift


def _bucket_metrics(rows: list[tuple]) -> dict:
    """rows: (model_p, side, outcome, paper_pnl). Returns metrics dict."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    wins = 0
    pnl = 0.0
    yes_count = 0
    sq_err = 0.0
    for model_p, side, outcome, paper_pnl in rows:
        o = 1 if outcome == "yes" else 0
        yes_count += o
        # Brier = (p_for_outcome - 1)^2. Equivalent: (model_p - o)^2 where
        # model_p is the predicted probability of YES.
        sq_err += (float(model_p) - o) ** 2
        won = (outcome == "yes" and side == "yes") or (outcome == "no" and side == "no")
        if won:
            wins += 1
        pnl += float(paper_pnl or 0)
    base_rate = yes_count / n
    naive_brier = sum((base_rate - (1 if oc == "yes" else 0)) ** 2
                      for _, _, oc, _ in rows) / n
    model_brier = sq_err / n
    bss = (1.0 - model_brier / naive_brier) if naive_brier > 0 else 0.0
    return {
        "n":           n,
        "wins":        wins,
        "losses":      n - wins,
        "win_rate":    wins / n,
        "pnl_usd":     round(pnl, 2),
        "model_brier": round(model_brier, 4),
        "naive_brier": round(naive_brier, 4),
        "bss":         round(bss, 4),
        "base_rate":   round(base_rate, 4),
    }


def collect_status(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        # Buckets present in the table
        buckets = conn.execute(
            """SELECT DISTINCT brain_id, COALESCE(chart_region, '?'), model_version
               FROM argus_paper_positions
               ORDER BY brain_id, chart_region, model_version"""
        ).fetchall()

        now = dt.datetime.now(dt.timezone.utc)
        bucket_out: list[dict] = []
        for brain_id, region, mv in buckets:
            # Open
            open_rows = conn.execute(
                """SELECT size_usd, entry_ts FROM argus_paper_positions
                   WHERE brain_id=? AND COALESCE(chart_region,'?')=? AND model_version=?
                   AND status='open'""",
                (brain_id, region, mv),
            ).fetchall()
            open_count = len(open_rows)
            open_deployed = sum(float(r[0] or 0) for r in open_rows)
            oldest_age_h = None
            if open_rows:
                oldest = min(
                    dt.datetime.fromisoformat(r[1].replace("Z", "+00:00"))
                    for r in open_rows
                    if r[1]
                )
                oldest_age_h = (now - oldest).total_seconds() / 3600.0

            # Resolved
            res_rows = conn.execute(
                """SELECT model_p, side, outcome, paper_pnl
                   FROM argus_paper_positions
                   WHERE brain_id=? AND COALESCE(chart_region,'?')=? AND model_version=?
                   AND status='resolved' AND outcome IN ('yes','no')""",
                (brain_id, region, mv),
            ).fetchall()
            m = _bucket_metrics(res_rows)

            drift_alert = (
                m["n"] >= BSS_MIN_N and m.get("bss", 0) < BSS_DRIFT_THRESHOLD
            )

            bucket_out.append({
                "brain_id":          brain_id,
                "chart_region":      region,
                "model_version":     mv,
                "open":              open_count,
                "deployed_usd":      round(open_deployed, 2),
                "oldest_age_h":      None if oldest_age_h is None else round(oldest_age_h, 1),
                "resolved":          m["n"],
                "wins":              m.get("wins", 0),
                "losses":            m.get("losses", 0),
                "win_rate":          m.get("win_rate"),
                "pnl_usd":           m.get("pnl_usd", 0.0),
                "model_brier":       m.get("model_brier"),
                "naive_brier":       m.get("naive_brier"),
                "bss":               m.get("bss"),
                "base_rate":         m.get("base_rate"),
                "drift_alert":       drift_alert,
            })

        # Totals
        all_open = conn.execute(
            "SELECT COALESCE(SUM(size_usd),0) FROM argus_paper_positions WHERE status='open'"
        ).fetchone()[0]
        all_resolved_pnl = conn.execute(
            "SELECT COALESCE(SUM(paper_pnl),0) FROM argus_paper_positions WHERE status='resolved'"
        ).fetchone()[0]
        all_resolved_n = conn.execute(
            "SELECT COUNT(*) FROM argus_paper_positions WHERE status='resolved'"
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "ts":                 dt.datetime.now(dt.timezone.utc).isoformat(),
        "bankroll_usd":       PAPER_BANKROLL,
        "deployed_usd_total": round(float(all_open or 0), 2),
        "headroom_usd":       round(PAPER_BANKROLL - float(all_open or 0), 2),
        "resolved_total":     all_resolved_n,
        "pnl_usd_total":      round(float(all_resolved_pnl or 0), 2),
        "buckets":            bucket_out,
    }


def render(s: dict) -> None:
    print(f"\n━━━ ARGUS STATUS — {s['ts']} ━━━")
    print(f"  bankroll=${s['bankroll_usd']:.0f}  "
          f"deployed=${s['deployed_usd_total']:.2f}  "
          f"headroom=${s['headroom_usd']:.2f}")
    print(f"  resolved_total={s['resolved_total']}  "
          f"realized_pnl=${s['pnl_usd_total']:+.2f}")
    if not s["buckets"]:
        print("\n  (no positions on record)")
        return
    print()
    hdr = (f"  {'brain':<8} {'region':<8} {'v':<3}  "
           f"{'open':>4} {'deploy$':>8} {'age_h':>6}  "
           f"{'res':>4} {'wins':>4} {'WR%':>5} {'pnl$':>8} "
           f"{'brier':>6} {'bss':>6}")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for b in s["buckets"]:
        wr_str = f"{b['win_rate']*100:.1f}" if b["win_rate"] is not None else "  -"
        age_str = f"{b['oldest_age_h']:.1f}" if b["oldest_age_h"] is not None else "  -"
        brier_str = f"{b['model_brier']:.3f}" if b["model_brier"] is not None else "  -"
        bss_str   = f"{b['bss']:+.3f}"        if b["bss"]         is not None else "  -"
        alert = "  ⚠ DRIFT" if b["drift_alert"] else ""
        print(f"  {b['brain_id']:<8} {b['chart_region']:<8} {b['model_version']:<3}  "
              f"{b['open']:>4} {b['deployed_usd']:>8.2f} {age_str:>6}  "
              f"{b['resolved']:>4} {b['wins']:>4} {wr_str:>5} "
              f"{b['pnl_usd']:>+8.2f} {brier_str:>6} {bss_str:>6}"
              f"{alert}")
    print()
    alerts = [b for b in s["buckets"] if b["drift_alert"]]
    if alerts:
        print(f"  🔴 DRIFT ALERT: {len(alerts)} bucket(s) below BSS={BSS_DRIFT_THRESHOLD}")
    elif any(b["resolved"] >= BSS_MIN_N for b in s["buckets"]):
        print(f"  ✓ all buckets with n ≥ {BSS_MIN_N} pass BSS ≥ {BSS_DRIFT_THRESHOLD}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    s = collect_status(CANDIDATES_DB())
    if a.json:
        print(json.dumps(s, indent=2, default=str))
    else:
        render(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
