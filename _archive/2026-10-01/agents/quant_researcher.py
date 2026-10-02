"""QUANT-RESEARCHER (Lens) — innait research tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Owns backtest harness + nightly walk-forward + OOS evidence ledger. Provides Sharpe/Calmar/drawdown data CFO consumes for capital allocation and CEO consumes for kill/merge decisions. VETOES live-flip requests lacking sufficient walk-forward evidence.

REPORTING / VETO
----------------
  Reports to: cfo
  Vetoes: live_flip_request, capital_increase_request

STATUS
------
SKELETON — implementation lands in Week 3-4 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="quant_researcher",
    persona="Lens",
    model="claude-sonnet-4-6",
    tier="research",
    reports_to="cfo",
    can_veto=("live_flip_request", "capital_increase_request"),
    system_prompt_path="agents/prompts/quant_researcher.txt",
)


class Agent(AgentProtocol):
    """Lens persona. See plan Section A.Research."""
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
