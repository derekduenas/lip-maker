"""COMPLIANCE (Counsel) — innait support tier.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Macro blackouts (FOMC/NFP/CPI). Jurisdiction checks (Polymarket US-only verification, Kraken Pro derivatives restrictions, Deribit US read-only). Audit-log integrity. Unconditional veto on compliance grounds.

REPORTING / VETO
----------------
  Reports to: cro
  Vetoes: regulatory, venue_jurisdiction, blackout_window

STATUS
------
SKELETON — implementation lands in Week 3-4 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="compliance",
    persona="Counsel",
    model="claude-sonnet-4-6",
    tier="support",
    reports_to="cro",
    can_veto=("regulatory", "venue_jurisdiction", "blackout_window"),
    system_prompt_path="agents/prompts/compliance.txt",
)


class Agent(AgentProtocol):
    """Counsel persona. See plan Section A.Support."""
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
