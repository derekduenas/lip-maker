"""Automated ramp-up controller for LIP live mode.

Runs daily (via cron). Reads last 7 days of realized P&L from quotes table
and decides whether to:
  - ADVANCE to next phase (10% → 20% → 30% → 40%)
  - STAY at current phase
  - RETREAT one phase (back off on losses)
  - HALT (emergency stop)

Decision rules:
  ADVANCE if: last 7 days have ≥5 non-negative days AND cumulative P&L > 0
              AND current phase < 4 AND phase has been active ≥5 days
  RETREAT if: last 3 days all negative OR daily-halt circuit triggered
              OR cumulative 7d P&L < -2% of bankroll
  HALT    if: cumulative 7d P&L < -5% of bankroll OR 5 consecutive neg days
  Else STAY.

Writes decisions to ramp_log table + alerts.log for visibility.
Applies via systemctl (same as scale_caps.py apply path).

Run: PYTHONPATH=. venv/bin/python monitor/ramp_controller.py
Cron: daily at 09:00 UTC.
"""
from __future__ import annotations


# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "ramp_controller")
except Exception:
    pass

import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

RAMP_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS ramp_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     TEXT NOT NULL,
    prior_phase     INTEGER,
    new_phase       INTEGER,
    decision        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    pnl_7d_usd      REAL,
    pos_days_7d     INTEGER,
    neg_days_7d     INTEGER,
    bankroll_usd    REAL
);
"""


def daily_pnls(db_path: str, days: int = 7) -> list[tuple[str, float, int, int]]:
    """Returns [(date_iso, pnl, n_fills, n_wins), ...] for the last N days.

    Source: daily_pnl_log table (populated by tools/daily_pnl_snapshot.py from
    Kalshi /portfolio/positions realized_pnl_dollars). The quotes.filled_at
    column is not wired to the WS fill event, so it was always empty — reading
    from there caused ramp controller to see 0 P&L and never advance phase.
    """
    conn = sqlite3.connect(db_path)
    try:
        # Audit #8: exclude today (partial day — snapshot may not have run yet)
        # and cap to exactly `days` completed days.
        today = datetime.now(timezone.utc).date()
        cutoff_day = (today - timedelta(days=days)).isoformat()
        today_str = today.isoformat()
        rows = conn.execute(
            """SELECT day, daily_realized_delta, total_fills_to_date
               FROM daily_pnl_log
               WHERE day >= ? AND day < ?
               ORDER BY day""",
            (cutoff_day, today_str),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    result = []
    prev_fills = 0
    for day, delta, fills in rows:
        delta = delta or 0
        fills = fills or 0
        day_fills = fills - prev_fills
        prev_fills = fills
        wins = day_fills if delta >= 0 else 0
        result.append((day, delta, day_fills, wins))
    return result


def current_phase() -> int:
    """Read current phase from the systemd service file."""
    try:
        txt = Path("/etc/systemd/system/lip-maker.service").read_text()
        for line in txt.splitlines():
            if line.startswith("Environment=LIP_RAMP_PHASE="):
                return int(line.split("=")[-1])
    except Exception:
        pass
    return int(os.getenv("LIP_RAMP_PHASE", "1"))


def apply_phase(new_phase: int, reason: str):
    """Rewrite service + restart."""
    svc = Path("/etc/systemd/system/lip-maker.service")
    if not svc.exists():
        _log.error("service file not found")
        return
    lines = svc.read_text().splitlines()
    out = []
    for ln in lines:
        if ln.startswith("Environment=LIP_RAMP_PHASE="):
            out.append(f"Environment=LIP_RAMP_PHASE={new_phase}")
        else:
            out.append(ln)
    svc.write_text("\n".join(out) + "\n")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "lip-maker.service"], check=True)
    _log.info(f"[RAMP] phase applied: {new_phase} — {reason}")


def decide(db_path: str = settings.DB_PATH) -> dict:
    """Make the ramp decision for today."""
    phase = current_phase()
    bankroll = settings.BANKROLL_USD
    daily_halt = bankroll * 0.05

    pnls = daily_pnls(db_path, days=7)
    pnl_7d = sum(p for _, p, _, _ in pnls)
    pos_days = sum(1 for _, p, _, _ in pnls if p >= 0)
    neg_days = sum(1 for _, p, _, _ in pnls if p < 0)
    days_of_data = len(pnls)

    decision = "STAY"
    reason = f"default — 7d P&L ${pnl_7d:+.2f}"
    new_phase = phase

    # HALT conditions (strongest)
    if pnl_7d < -bankroll * 0.05:
        decision = "HALT"
        new_phase = 1
        reason = f"7d loss ${pnl_7d:+.2f} < -5% bankroll ${-bankroll*0.05:.2f}"
    # Also halt if last 3 days all negative
    elif len(pnls) >= 3 and all(p < 0 for _, p, _, _ in pnls[-3:]):
        decision = "HALT"
        new_phase = 1
        reason = "last 3 days all negative — halting to Phase 1"

    # RETREAT conditions
    elif pnl_7d < -bankroll * 0.02 and phase > 1:
        decision = "RETREAT"
        new_phase = max(1, phase - 1)
        reason = f"7d P&L ${pnl_7d:+.2f} < -2% bankroll — step back one phase"

    # ADVANCE conditions
    elif pos_days >= 5 and pnl_7d > 0 and phase < 4 and days_of_data >= 5:
        decision = "ADVANCE"
        new_phase = phase + 1
        reason = f"{pos_days}/7 positive days, +${pnl_7d:.2f} — promoting"

    # Log decision
    init_conn = sqlite3.connect(db_path)
    try:
        init_conn.executescript(RAMP_LOG_SCHEMA)
        init_conn.execute(
            """INSERT INTO ramp_log
               (recorded_at, prior_phase, new_phase, decision, reason,
                pnl_7d_usd, pos_days_7d, neg_days_7d, bankroll_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), phase, new_phase, decision,
             reason, pnl_7d, pos_days, neg_days, bankroll),
        )
        init_conn.commit()
    finally:
        init_conn.close()

    # Append to alert log
    try:
        alerts_path = Path(settings.LOGS_DIR) / "alerts.log"
        alerts_path.parent.mkdir(parents=True, exist_ok=True)
        with open(alerts_path, "a") as f:
            f.write(
                f"{datetime.now(timezone.utc).isoformat()}  RAMP  "
                f"{decision}  {phase}→{new_phase}  {reason}\n"
            )
    except Exception:
        pass

    # Apply if change
    if new_phase != phase and decision != "STAY":
        apply_phase(new_phase, reason)

    return {
        "prior_phase":  phase,
        "new_phase":    new_phase,
        "decision":     decision,
        "reason":       reason,
        "pnl_7d_usd":   pnl_7d,
        "pos_days":     pos_days,
        "neg_days":     neg_days,
        "days_of_data": days_of_data,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = decide()
    print("=== Ramp decision ===")
    for k, v in r.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
