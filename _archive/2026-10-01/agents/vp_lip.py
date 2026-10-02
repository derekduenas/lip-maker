"""VP-LIP (Harvester) — innait line VP.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Maximize Kalshi LIP rebate yield until Sept 1, 2026 sunset. Manage graceful wind-down. Wraps existing 4-engine LIP stack. Declining cash-flow silo — optimize for cash extraction, not capability expansion.

REPORTING / VETO
----------------
  Reports to: ceo
  Vetoes: lip_market_entry

STATUS
------
SKELETON — implementation lands in Week 5-6 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="vp_lip",
    persona="Harvester",
    model="claude-sonnet-4-6",
    tier="vp",
    reports_to="ceo",
    can_veto=("lip_market_entry",),
    system_prompt_path="agents/prompts/vp_lip.txt",
)


class Agent(AgentProtocol):
    """Harvester persona. See plan Section A.Line VP."""
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
