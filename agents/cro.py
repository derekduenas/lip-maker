"""CRO (Sentinel) — innait executive tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Constitutional risk enforcement. Pre-trade approval — every trade proposal must pass risk/sentinel.py:approve(). Post-trade markout review. Owns the kill-switch. VETO IS UNCONDITIONAL — even the human owner cannot bypass without 24h cooling-off + written override logged to postmortem_log.

REPORTING / VETO
----------------
  Reports to: ceo
  Vetoes: trade_proposal, constitutional_breach, venue_health

STATUS
------
SKELETON — implementation lands in Week 3-4 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="cro",
    persona="Sentinel",
    model="claude-sonnet-4-6",
    tier="executive",
    reports_to="ceo",
    can_veto=("trade_proposal", "constitutional_breach", "venue_health"),
    system_prompt_path="agents/prompts/cro.txt",
)


class Agent(AgentProtocol):
    """Sentinel persona. See plan Section A.Executive."""
    SPEC = SPEC

    def read_inputs(self, blackboard) -> dict:
        # TODO Week 3-4: wire to blackboard tables
        raise NotImplementedError

    def propose(self, inputs):
        # TODO Week 3-4: implement persona reasoning
        raise NotImplementedError

    def report(self, output, blackboard) -> None:
        # TODO Week 3-4: persist output to blackboard
        raise NotImplementedError
