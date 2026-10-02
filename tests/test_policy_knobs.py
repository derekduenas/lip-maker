"""Every LIP_* knob named in the deployed APEX config must be read by code.

A variable set in deploy/apex/lip-unattended.service.d/policy.conf or
deploy/apex/watchdog.env.example that no Python source mentions is a knob
the operator believes is active but that does nothing. This test parses
both files and requires each LIP_* name to appear as a string literal in
non-test, non-archive Python source.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "deploy" / "apex" / "lip-unattended.service.d" / "policy.conf"
WATCHDOG_ENV = ROOT / "deploy" / "apex" / "watchdog.env.example"
SKIP_DIRS = {"tests", "_archive", "archive", ".git", "__pycache__", ".venv", "venv"}
NAME = re.compile(r"\bLIP_[A-Z0-9_]*[A-Z0-9]\b")   # "LIP_WD_*" (a glob) is not a name

def _knobs() -> list[str]:
    names: set[str] = set()
    for path in (POLICY, WATCHDOG_ENV):
        names.update(NAME.findall(path.read_text(encoding="utf-8")))
    return sorted(names)


def _literals() -> set[str]:
    out: set[str] = set()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).parts
        if any(p in SKIP_DIRS for p in rel[:-1]):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.add(node.value)
    return out


LITERALS = _literals()


def test_config_files_name_some_knobs():
    assert len(_knobs()) > 20


@pytest.mark.parametrize("name", _knobs())
def test_knob_is_read_by_code(name):
    assert name in LITERALS, f"{name} is set in deploy/apex but no Python source reads it"
