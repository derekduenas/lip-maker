"""Preview + apply bankroll-scaled risk caps.

Usage:
    PYTHONPATH=. venv/bin/python tools/scale_caps.py --preview
    PYTHONPATH=. venv/bin/python tools/scale_caps.py --apply 5000 --ramp 1

The service reads `LIP_BANKROLL` and `LIP_RAMP_PHASE` env vars on start.
This tool edits `/etc/systemd/system/lip-maker.service` to set those,
daemon-reloads, and restarts the service.

Ramp-up schedule (days 1-N after going live):
    Phase 1: 10% bankroll total gross   (conservative)
    Phase 2: 20% bankroll               (proven)
    Phase 3: 30%                         (scaling)
    Phase 4: 40%                         (mature)

Rule: only advance a phase when PRIOR PHASE HAD 7 CONSECUTIVE DAYS OF
NON-NEGATIVE P&L. Otherwise stay or step back.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def preview(bankroll: float, ramp: int) -> dict:
    fraction = {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.40}[max(1, min(4, ramp))]
    total_gross = bankroll * fraction
    per_mkt = max(15.0, total_gross * 0.05)
    return {
        "bankroll":          bankroll,
        "ramp_phase":        ramp,
        "ramp_fraction":     f"{fraction*100:.0f}%",
        "total_gross_cap":   f"${total_gross:.0f}",
        "per_market_cap":    f"${per_mkt:.0f}",
        "net_inv_per_mkt":   f"${per_mkt * 0.5:.0f}",
        "total_net_cap":     f"${total_gross * 0.25:.0f}",
        "daily_loss_limit":  f"${bankroll * 0.05:.0f}",
        "session_loss_cap":  f"${bankroll * 0.10:.0f}",
        "single_loss_cap":   f"${bankroll * 0.02:.2f}",
    }


def apply(bankroll: float, ramp: int, paper: bool = False) -> None:
    """Rewrite the service file with new env and restart."""
    svc_path = "/etc/systemd/system/lip-maker.service"
    if not Path(svc_path).exists():
        print(f"ERROR: {svc_path} not found. Run on server.")
        return
    lines = Path(svc_path).read_text().splitlines()
    out = []
    seen_bankroll = False
    seen_ramp = False
    seen_paper = False
    for ln in lines:
        if ln.startswith("Environment=LIP_BANKROLL="):
            out.append(f"Environment=LIP_BANKROLL={bankroll:.0f}")
            seen_bankroll = True
        elif ln.startswith("Environment=LIP_RAMP_PHASE="):
            out.append(f"Environment=LIP_RAMP_PHASE={ramp}")
            seen_ramp = True
        elif ln.startswith("Environment=LIP_PAPER="):
            out.append(f"Environment=LIP_PAPER={'true' if paper else 'false'}")
            seen_paper = True
        else:
            out.append(ln)
    # Append any missing env lines before [Install]
    final = []
    added = False
    for ln in out:
        if ln.strip().startswith("[Install]") and not added:
            if not seen_bankroll:
                final.append(f"Environment=LIP_BANKROLL={bankroll:.0f}")
            if not seen_ramp:
                final.append(f"Environment=LIP_RAMP_PHASE={ramp}")
            if not seen_paper:
                final.append(f"Environment=LIP_PAPER={'true' if paper else 'false'}")
            added = True
        final.append(ln)

    Path(svc_path).write_text("\n".join(final) + "\n")
    print(f"wrote {svc_path} with bankroll={bankroll:.0f}, ramp={ramp}, paper={paper}")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "lip-maker.service"], check=True)
    print("service restarted")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--preview", action="store_true")
    p.add_argument("--apply",   type=float, metavar="BANKROLL",
                   help="Set bankroll (USD) and apply")
    p.add_argument("--ramp",    type=int, default=1, choices=[1, 2, 3, 4])
    p.add_argument("--paper",   action="store_true",
                   help="Also force paper mode (safe default)")
    a = p.parse_args()

    if a.apply is not None:
        from pprint import pprint
        print("Preview of new caps:")
        pprint(preview(a.apply, a.ramp))
        print()
        apply(a.apply, a.ramp, paper=a.paper)
    else:
        # Just preview for all phases
        for bk in [80, 1000, 5000, 10000, 25000]:
            print(f"\n-- bankroll ${bk} --")
            for phase in [1, 2, 3, 4]:
                pv = preview(bk, phase)
                print(f"  phase {phase} ({pv['ramp_fraction']:>4s}):  "
                      f"total=${pv['total_gross_cap']:>5s}  "
                      f"per_mkt=${pv['per_market_cap']:>5s}  "
                      f"daily_loss_halt=${pv['daily_loss_limit']:>5s}")


if __name__ == "__main__":
    main()
