"""CEO (Innait-Prime) — innait executive tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Synthesize VP reports → firm-wide strategy. Allocate capital across silos. Final veto on strategic-directive scope. Cannot override CRO risk vetoes — may only request re-evaluation with new evidence.

REPORTING / VETO
----------------
  Reports to: human owner
  Vetoes: strategy_directive, kill_decision

STATUS
------
SKELETON — implementation lands in Week 11-12 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="ceo",
    persona="Innait-Prime",
    model="claude-opus-4-7",
    tier="executive",
    reports_to=None,
    can_veto=("strategy_directive", "kill_decision"),
    system_prompt_path="agents/prompts/ceo.txt",
)


class Agent(AgentProtocol):
    """Innait-Prime persona. See plan Section A.Executive."""
    SPEC = SPEC

    def read_inputs(self, blackboard) -> dict:
        # TODO Week 11-12: wire to blackboard tables
        raise NotImplementedError

    def propose(self, inputs):
        # TODO Week 11-12: implement persona reasoning
        raise NotImplementedError

    def report(self, output, blackboard) -> None:
        # TODO Week 11-12: persist output to blackboard
        raise NotImplementedError
