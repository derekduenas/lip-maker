"""Review (2026-10-01): the legacy Polymarket runner is paper-only.

polymarket/run_pm.py with PM_PAPER=false sent real orders through
PMQuoteManager.orders.create(). mm.unattended is the only path allowed to
reach a live venue, so both the runner and the quote manager refuse live.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
QM_PATH = ROOT / "polymarket" / "execution" / "pm_quote_manager.py"


@pytest.fixture
def pm_qm_module(monkeypatch):
    # The polymarket_us SDK is not a dependency of this repo; stub it.
    stub = types.ModuleType("polymarket_us")
    stub.PolymarketUS = object
    monkeypatch.setitem(sys.modules, "polymarket_us", stub)
    spec = importlib.util.spec_from_file_location("_pm_qm_under_test", QM_PATH)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_pm_qm_under_test", mod)   # dataclasses need it
    spec.loader.exec_module(mod)
    return mod


def test_pm_quote_manager_refuses_live(pm_qm_module):
    client = MagicMock()
    with pytest.raises(RuntimeError, match="paper-only"):
        pm_qm_module.PMQuoteManager(client, paper=False)
    client.orders.create.assert_not_called()
    client.orders.list.assert_not_called()


def test_pm_quote_manager_paper_still_constructs(pm_qm_module):
    qm = pm_qm_module.PMQuoteManager(MagicMock(), paper=True)
    assert qm.paper is True


def test_run_pm_refuses_pm_paper_false():
    env = dict(os.environ, PM_PAPER="false")
    r = subprocess.run([sys.executable, str(ROOT / "polymarket" / "run_pm.py")],
                       cwd=str(ROOT / "polymarket"), env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "paper-only" in (r.stderr + r.stdout)
