"""Review fixes: watchdog deploy files stay consistent with the code."""
from pathlib import Path

from mm.safety import lip_watchdog as wd

APEX = Path(__file__).resolve().parents[1] / "deploy" / "apex"


def _env_example():
    env = {}
    for line in (APEX / "watchdog.env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def test_env_example_parses_and_is_paper_safe(tmp_path):
    env = dict(_env_example(), LIP_WD_STATE_DIR=str(tmp_path))
    cfg = wd.Config(env)
    assert not wd.config_armed(cfg)
    assert cfg.cancel_scope == "ours" and cfg.coid_prefixes == ("LIP-",)
    assert cfg.stop_cmd == "systemctl stop lip-unattended"
    assert cfg.max_capital >= 1500  # engine LIP_BANKROLL=1500


def test_unit_restarts_forever():
    unit = (APEX / "lip-watchdog.service").read_text()
    assert "Restart=always" in unit and "StartLimitIntervalSec=0" in unit
    assert "polkit" in unit


def test_readme_documents_reset_scope_and_arming():
    text = (APEX / "README.md").read_text()
    assert "once per second and" not in text
    for needle in ("--reset", "LIP_WD_CANCEL_SCOPE", "engine live but watchdog unarmed", "polkit"):
        assert needle in text
