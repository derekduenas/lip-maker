"""CFO (Ledger) — innait executive tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
P&L attribution per silo. Capital allocation. Cash-flow forecasting. Tax-lot tracking. Enforces $85/mo LLM hard cap. Can refuse capital deployment if silo Sharpe < threshold or unreconciled drift > 1%.

REPORTING / VETO
----------------
  Reports to: ceo
  Vetoes: capital_allocation, cost_governance

STATUS
------
SKELETON — implementation lands in Week 3-4 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="cfo",
    persona="Ledger",
    model="claude-sonnet-4-6",
    tier="executive",
    reports_to="ceo",
    can_veto=("capital_allocation", "cost_governance"),
    system_prompt_path="agents/prompts/cfo.txt",
)


class Agent(AgentProtocol):
    """Ledger persona. See plan Section A.Executive."""
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
