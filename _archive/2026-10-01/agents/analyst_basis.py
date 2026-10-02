"""ANALYST-BASIS (Basis) — innait analyst tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Crypto analyst → VP-Crypto. Funding-rate time-series, perp/spot basis, IV surface deltas. Writes to signals_log every 30s.

REPORTING / VETO
----------------
  Reports to: vp_crypto
  Vetoes: (none)

STATUS
------
SKELETON — implementation lands in Week 9-10 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="analyst_basis",
    persona="Basis",
    model="claude-haiku-4-5",
    tier="analyst",
    reports_to="vp_crypto",
    can_veto=(),
    system_prompt_path="agents/prompts/analyst_basis.txt",
)


class Agent(AgentProtocol):
    """Basis persona. See plan Section A.Analyst."""
    SPEC = SPEC

    def read_inputs(self, blackboard) -> dict:
        # TODO Week 9-10: wire to blackboard tables
        raise NotImplementedError

    def propose(self, inputs):
        # TODO Week 9-10: implement persona reasoning
        raise NotImplementedError

    def report(self, output, blackboard) -> None:
        # TODO Week 9-10: persist output to blackboard
        raise NotImplementedError
