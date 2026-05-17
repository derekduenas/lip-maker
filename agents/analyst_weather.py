"""ANALYST-WEATHER (Weather) — innait analyst tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Cross-silo regime analyst → all three VPs + CRO. Common-good signal: volatility regime, macro blackout calendar, cross-asset stress index. The ONLY cross-silo analyst.

REPORTING / VETO
----------------
  Reports to: ceo
  Vetoes: (none)

STATUS
------
SKELETON — implementation lands in Week 5-6 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="analyst_weather",
    persona="Weather",
    model="claude-haiku-4-5",
    tier="analyst",
    reports_to="ceo",
    can_veto=(),
    system_prompt_path="agents/prompts/analyst_weather.txt",
)


class Agent(AgentProtocol):
    """Weather persona. See plan Section A.Analyst."""
    SPEC = SPEC

    def read_inputs(self, blackboard) -> dict:
        # TODO Week 5-6: wire to blackboard tables
        raise NotImplementedError

    def propose(self, inputs):
        # TODO Week 5-6: implement persona reasoning
        raise NotImplementedError

    def report(self, output, blackboard) -> None:
        # TODO Week 5-6: persist output to blackboard
        raise NotImplementedError
