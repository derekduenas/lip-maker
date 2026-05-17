"""ANALYST-TICK (Tick) — innait analyst tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Microstructure analyst → VP-LIP. Owns VPIN, microprice, order-flow toxicity. Writes structured-JSON signals to signals_log every 30s.

REPORTING / VETO
----------------
  Reports to: vp_lip
  Vetoes: (none)

STATUS
------
SKELETON — implementation lands in Week 5-6 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="analyst_tick",
    persona="Tick",
    model="claude-haiku-4-5",
    tier="analyst",
    reports_to="vp_lip",
    can_veto=(),
    system_prompt_path="agents/prompts/analyst_tick.txt",
)


class Agent(AgentProtocol):
    """Tick persona. See plan Section A.Analyst."""
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
