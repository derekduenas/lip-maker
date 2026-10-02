"""Integration fixes, deploy files: the APEX ExecStart override keeps paper
forced, the droplet env template does not override the pinned bankroll, and
the container uses the Python the requirements were pinned on."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE = ROOT / "deploy/apex/lip-unattended.service.d/override.conf"
ENV_EXAMPLE = ROOT / "deploy/droplet/lip-maker.env.example"


def _exec_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("ExecStart=")]


def test_override_execstart_forces_paper_on_the_command_line():
    text = OVERRIDE.read_text()
    lines = _exec_lines(text)
    # the drop-in clears the base ExecStart and sets its own
    assert lines[0] == "ExecStart="
    real = [line for line in lines if line != "ExecStart="]
    assert len(real) == 1
    assert real[0].startswith("ExecStart=/usr/bin/env LIP_FORCE_PAPER=1 LIP_PAPER=true ")
    assert "/opt/lip-maker/.venv/bin/python -m mm.unattended --run" in real[0]
    assert "Environment=LIP_FORCE_PAPER=1" in text
    assert "LIP_PAPER=false" not in text


def test_droplet_env_template_does_not_set_bankroll():
    text = ENV_EXAMPLE.read_text()
    active = [line.strip() for line in text.splitlines()
              if line.strip() and not line.strip().startswith("#")]
    assert not any(line.startswith("LIP_BANKROLL=") for line in active)
    assert "LIP_BANKROLL" in text  # the comment explaining why it is absent


def test_dockerfile_python_matches_requirements_pins():
    docker = (ROOT / "Dockerfile").read_text()
    reqs = (ROOT / "requirements.txt").read_text()
    assert "FROM python:3.13-slim" in docker
    assert "Python 3.13" in reqs


def test_apex_readme_documents_operator_knobs():
    text = (ROOT / "deploy/apex/README.md").read_text()
    for needle in ("LIP_LOAD_DOTENV=1", "LIP_FORCE_PAPER", "LIP_STATE_FILE",
                   "python -m mm.unattended --reset-kill"):
        assert needle in text


def test_session_gates_docstring_claims_no_unbacked_figures():
    import mm.session_gates as g
    for figure in ("7,035", "$924", "$238"):
        assert figure not in g.__doc__
