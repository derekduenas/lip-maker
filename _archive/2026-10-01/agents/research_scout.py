"""RESEARCH-SCOUT (Recon) — innait research tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Systematic edge discovery. Monitors arXiv q-fin, SSRN, FinTwit, Kalshi forum, CFTC filings, Polymarket community. Every Friday 17:00 ET surfaces 3-5 opportunity candidates to CEO. Lifecycle: surface → CEO triage → paper-shadow → graduate. Proposes; does not dispose.

REPORTING / VETO
----------------
  Reports to: ceo
  Vetoes: (none)

STATUS
------
SKELETON — implementation lands in Week 11-12 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="research_scout",
    persona="Recon",
    model="claude-haiku-4-5",
    tier="research",
    reports_to="ceo",
    can_veto=(),
    system_prompt_path="agents/prompts/research_scout.txt",
)


class Agent(AgentProtocol):
    """Recon persona. See plan Section A.Research."""
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
