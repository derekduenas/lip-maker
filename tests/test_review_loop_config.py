"""Review fixes, deploy config: capital pinned under the watchdog cap, paper
forced in the unit and the APEX policy drop-in."""
from pathlib import Path

from mm.risk import Limits
from decimal import Decimal

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "deploy/apex/lip-unattended.service.d/policy.conf"
UNIT = ROOT / "deploy/lip-unattended.service"


def test_policy_pins_bankroll_under_the_watchdog_cap():
    text = POLICY.read_text()
    assert "Environment=LIP_BANKROLL=1500" in text
    lim = Limits.from_capital(Decimal("1500"))
    # whole engine budget (gross x 0.95 alloc fraction) under LIP_WD_MAX_CAPITAL_USD=1600
    assert float(lim.gross_usd) * 0.95 < 1600


def test_deploy_units_force_paper():
    unit = UNIT.read_text()
    policy = POLICY.read_text()
    assert "Environment=LIP_FORCE_PAPER=1" in policy
    assert "Environment=LIP_FORCE_PAPER=1" in unit
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "LIP_FORCE_PAPER=1" in exec_line and "LIP_PAPER=true" in exec_line
    assert "LIP_PAPER=false" not in unit + policy
