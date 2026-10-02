"""VP-MACRO (Compass) — innait line VP.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Trade Kalshi Fed contracts (KXFEDDECISION) against CME ZQ futures + ForecastEx. Harvest dislocation around FOMC announcements. Highest-quality edge per research (score 8/10, scales to $1M).

REPORTING / VETO
----------------
  Reports to: ceo
  Vetoes: macro_trade_proposal

STATUS
------
SKELETON — implementation lands in Week 7-8 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="vp_macro",
    persona="Compass",
    model="claude-sonnet-4-6",
    tier="vp",
    reports_to="ceo",
    can_veto=("macro_trade_proposal",),
    system_prompt_path="agents/prompts/vp_macro.txt",
)


class Agent(AgentProtocol):
    """Compass persona. See plan Section A.Line VP."""
    SPEC = SPEC

    def read_inputs(self, blackboard) -> dict:
        # TODO Week 7-8: wire to blackboard tables
        raise NotImplementedError

    def propose(self, inputs):
        # TODO Week 7-8: implement persona reasoning
        raise NotImplementedError

    def report(self, output, blackboard) -> None:
        # TODO Week 7-8: persist output to blackboard
        raise NotImplementedError
