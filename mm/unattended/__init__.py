"""Unattended paper/demo loop. See ``service`` for the process entrypoint."""
from mm.unattended.feed import DEMO_WS_URL
from mm.unattended.service import UnattendedRefused, assert_paper_demo

__all__ = ["DEMO_WS_URL", "UnattendedRefused", "assert_paper_demo"]
