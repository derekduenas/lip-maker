"""ANALYST-DRIFT (Drift) — innait analyst tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Macro analyst → VP-Macro. Fed minutes parsing, ZQ curve shape, ForecastEx spread tracking. Writes to signals_log on macro events.

REPORTING / VETO
----------------
  Reports to: vp_macro
  Vetoes: (none)

STATUS
------
SKELETON — implementation lands in Week 7-8 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="analyst_drift",
    persona="Drift",
    model="claude-haiku-4-5",
    tier="analyst",
    reports_to="vp_macro",
    can_veto=(),
    system_prompt_path="agents/prompts/analyst_drift.txt",
)


class Agent(AgentProtocol):
    """Drift persona. See plan Section A.Analyst."""
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
