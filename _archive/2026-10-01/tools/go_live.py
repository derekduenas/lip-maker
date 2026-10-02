"""One-button LIP paper → live promotion.

Pre-flight checks (ALL must pass):
  [GATE 1] Paper has been running ≥24 hours uninterrupted
  [GATE 2] ≥50 markets with ≥10% projected share in last 6h
  [GATE 3] Projected paper $/day ≥ $100 (below that, not worth live risk)
  [GATE 4] No stale-inventory anomalies (resting count ≈ 2 × quoted markets)
  [GATE 5] lip_snapshots writing at expected rate (≥100 snaps/min across all)
  [GATE 6] Bankroll argument provided (safety — no accidental use of default)

If all pass → rewrites /etc/systemd/system/lip-maker.service with:
  LIP_PAPER=false
  LIP_BANKROLL=<amount>
  LIP_RAMP_PHASE=1       # always start conservative
And restarts the service.

Run:
    PYTHONPATH=. venv/bin/python tools/go_live.py --check        # dry run, show gates
    PYTHONPATH=. venv/bin/python tools/go_live.py --apply 5000   # execute flip

SAFETY:
  - Phase 1 only (10% bankroll) — ramp_controller.py handles advancement
  - Abort on ANY failed gate (fail-closed)
  - Logs decision to alerts.log
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from monitor.alerts import alert

_log = logging.getLogger(__name__)


def gate_1_uptime() -> tuple[bool, str]:
    """Paper service has been running ≥24h."""
    try:
        r = subprocess.run(
            ["systemctl", "show", "lip-maker.service",
             "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=3,
        )
        ts_s = r.stdout.strip().split("=")[-1]
        if not ts_s:
            return False, "service not started"
        # systemd format: "Sun 2026-04-19 20:35:47 UTC"
        from datetime import datetime as dt
        parts = ts_s.split()
        if len(parts) < 4:
            return False, f"unparseable timestamp: {ts_s}"
        dt_s = f"{parts[1]} {parts[2]}"
        active_since = dt.strptime(dt_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - active_since).total_seconds() / 3600
        if age_h < 24:
            return False, f"only {age_h:.1f}h uptime (need ≥24h)"
        return True, f"{age_h:.1f}h uptime"
    except Exception as e:
        return False, f"check failed: {e}"


def gate_2_winning_markets(db_path: str = settings.DB_PATH) -> tuple[bool, str]:
    """≥50 markets with ≥10% share in last 6h."""
    since = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT COUNT(*) FROM (
                 SELECT market_ticker,
                        AVG(CASE WHEN snapshot_valid=1 THEN our_score END) / 2.0 AS share
                 FROM lip_snapshots
                 WHERE captured_at >= ?
                 GROUP BY market_ticker
                 HAVING share >= 0.10)""",
            (since,),
        ).fetchone()
        n = rows[0] if rows else 0
    finally:
        conn.close()
    ok = n >= 50
    return ok, f"{n} markets ≥10% share (need ≥50)"


def gate_3_projection(db_path: str = settings.DB_PATH) -> tuple[bool, str]:
    """Projected paper $/day ≥ $100."""
    since = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT s.market_ticker,
                      AVG(CASE WHEN snapshot_valid=1 THEN our_score END) / 2.0 AS share,
                      SUM(s.snapshot_valid) * 1.0 / COUNT(*) AS valid_pct,
                      p.reward_per_day_usd
               FROM lip_snapshots s LEFT JOIN lip_programs p ON s.market_ticker = p.market_ticker
               WHERE s.captured_at >= ?
               GROUP BY s.market_ticker""",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    total = sum((r[1] or 0) * (r[2] or 0) * (r[3] or 0) for r in rows)
    ok = total >= 100
    return ok, f"projected ${total:.2f}/day (need ≥$100)"


def gate_4_inventory_health(db_path: str = settings.DB_PATH) -> tuple[bool, str]:
    """Resting-order count should be ~2× active quoted markets."""
    conn = sqlite3.connect(db_path)
    try:
        resting = conn.execute(
            "SELECT COUNT(*) FROM quotes WHERE status = 'resting'").fetchone()[0]
        n_markets = conn.execute(
            "SELECT COUNT(DISTINCT market_ticker) FROM quotes WHERE status = 'resting'"
        ).fetchone()[0]
    finally:
        conn.close()
    if n_markets == 0:
        return False, "no active quotes"
    ratio = resting / n_markets
    ok = 1.8 <= ratio <= 2.2
    return ok, f"{resting} resting / {n_markets} markets = {ratio:.2f} (want 2.0)"


def gate_5_snapshot_rate(db_path: str = settings.DB_PATH) -> tuple[bool, str]:
    """≥100 snapshots/min in the last 5 min."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM lip_snapshots WHERE captured_at >= ?",
            (since,),
        ).fetchone()[0]
    finally:
        conn.close()
    rate = n / 5.0
    ok = rate >= 100
    return ok, f"{rate:.0f} snapshots/min (need ≥100)"


def run_gates(db_path: str = settings.DB_PATH) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Run all gates. Returns (all_pass, [(gate_name, pass, detail), ...])."""
    gates = [
        ("uptime_24h",      gate_1_uptime()),
        ("winning_markets", gate_2_winning_markets(db_path)),
        ("projection",      gate_3_projection(db_path)),
        ("inventory_health",gate_4_inventory_health(db_path)),
        ("snapshot_rate",   gate_5_snapshot_rate(db_path)),
    ]
    all_pass = all(g[1][0] for g in gates)
    return all_pass, [(n, p, d) for n, (p, d) in gates]


def apply_live(bankroll: float):
    """Rewrite service file for live mode Phase 1 at given bankroll + restart."""
    svc = Path("/etc/systemd/system/lip-maker.service")
    lines = svc.read_text().splitlines()
    out = []
    seen = {"PAPER": False, "BANKROLL": False, "RAMP": False}
    for ln in lines:
        if ln.startswith("Environment=LIP_PAPER="):
            out.append("Environment=LIP_PAPER=false"); seen["PAPER"] = True
        elif ln.startswith("Environment=LIP_BANKROLL="):
            out.append(f"Environment=LIP_BANKROLL={bankroll:.0f}"); seen["BANKROLL"] = True
        elif ln.startswith("Environment=LIP_RAMP_PHASE="):
            out.append("Environment=LIP_RAMP_PHASE=1"); seen["RAMP"] = True
        else:
            out.append(ln)
    # Insert any missing env lines
    final = []
    added = False
    for ln in out:
        if ln.strip().startswith("[Install]") and not added:
            if not seen["PAPER"]:    final.append("Environment=LIP_PAPER=false")
            if not seen["BANKROLL"]: final.append(f"Environment=LIP_BANKROLL={bankroll:.0f}")
            if not seen["RAMP"]:     final.append("Environment=LIP_RAMP_PHASE=1")
            added = True
        final.append(ln)
    svc.write_text("\n".join(final) + "\n")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "lip-maker.service"], check=True)
    alert("CRITICAL", "go_live", f"LIP flipped to LIVE — bankroll=${bankroll:.0f} phase=1")


def _gate0_quant_check(days: int = 14) -> tuple[bool, str]:
    """Phase 3 quantitative gate — wraps tools/go_live_check.py.

    Returns (passed, detail). passed=False covers all of:
      - run_check import failure
      - run_check exception during evaluation  (P1-5 fix 2026-05-14)
      - insufficient data
      - any gate failure
    All paths return False — fail-closed semantics. The script can no longer
    crash with a SystemExit on a DB lock or schema-drift exception during
    gate evaluation; instead the gate reports a failure and the caller can
    refuse to apply.
    """
    try:
        from tools.go_live_check import run_check
    except Exception as e:
        return False, f"go_live_check import failed: {e}"
    try:
        rep = run_check(days=days)
    except Exception as e:
        return False, f"run_check exception: {type(e).__name__}: {e}"
    if rep.insufficient:
        n_partial = sum(1 for g in rep.gates if g.insufficient_data)
        return False, (f"insufficient data: {n_partial}/{len(rep.gates)} gates "
                       f"need more samples (markout, sharpe, fills, basis)")
    if not rep.overall_pass:
        n_fail = sum(1 for g in rep.gates if not g.passed and not g.insufficient_data)
        return False, f"{n_fail}/{len(rep.gates)} quant gates failed"
    return True, f"all 5 quant gates pass over {days}d window"


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="dry run — show gate status")
    p.add_argument("--apply", type=float, metavar="BANKROLL",
                   help="execute flip with given bankroll USD")
    p.add_argument("--force", action="store_true",
                   help="apply even if gates fail (DANGEROUS)")
    p.add_argument("--quant-days", type=int, default=14,
                   help="lookback for the Phase 3 quant gate (default 14)")
    a = p.parse_args()

    # Gate 0 (quant): the 5-gate Phase 3 check — markout, Sharpe, drawdown,
    # fill_rate, basis_residual. This is the math protection added in the
    # 2026-05-14 rebuild. Without it, the operational gates below cannot
    # tell whether the system is statistically profitable, only that it is
    # operationally healthy. BOTH must pass.
    quant_pass, quant_detail = _gate0_quant_check(days=a.quant_days)

    all_pass, results = run_gates()
    print("\n═══════════════════════════════════════════════════")
    print("  GO-LIVE PRE-FLIGHT CHECKS")
    print("═══════════════════════════════════════════════════")
    mark = "✅" if quant_pass else "❌"
    print(f"  {mark} quant_gates       {quant_detail}")
    for name, passed, detail in results:
        mark = "✅" if passed else "❌"
        print(f"  {mark} {name:<18s}  {detail}")
    print()

    all_pass = all_pass and quant_pass

    if all_pass:
        print("  ALL GATES PASSED — system ready for live flip")
    else:
        n_fail = sum(1 for _, p, _ in results if not p)
        if not quant_pass:
            n_fail += 1
        print(f"  {n_fail} gate(s) failed — cannot flip live safely")
        print(f"  Run `python tools/go_live_check.py --days {a.quant_days}` for quant detail")

    if a.apply is not None:
        if not all_pass and not a.force:
            print("\nABORTING — gates failed. Use --force to override (dangerous).")
            return
        if a.apply < 100:
            print(f"ABORTING — bankroll ${a.apply} too small for live mode")
            return
        apply_live(a.apply)
        print(f"\n✓ Service restarted with LIP_PAPER=false LIP_BANKROLL={a.apply:.0f} PHASE=1")
        print("  Monitor via: cd /root/btc-daily && python tools/innait_status.py")


if __name__ == "__main__":
    main()
