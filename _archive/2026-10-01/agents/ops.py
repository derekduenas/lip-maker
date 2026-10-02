"""OPS (Concierge) — innait support tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Venue health monitoring, API rate-limit tracking, restart-flap watcher, deployment supervisor. Can halt trading on a venue when its API/health degrades.

REPORTING / VETO
----------------
  Reports to: cfo
  Vetoes: venue_health, deployment_halt

STATUS
------
SKELETON — implementation lands in Week 5-6 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="ops",
    persona="Concierge",
    model="claude-sonnet-4-6",
    tier="support",
    reports_to="cfo",
    can_veto=("venue_health", "deployment_halt"),
    system_prompt_path="agents/prompts/ops.txt",
)


class Agent(AgentProtocol):
    """Concierge persona. See plan Section A.Support."""
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
