"""VP-CRYPTO (Helix) — innait line VP.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Section A)

MANDATE
-------
Funding-rate arb on Kraken perps. Kalshi crypto strikes (KXBTC*, KXETH*) against Deribit IV surface. Hates directional crypto bets; lives in basis trades and carry.

REPORTING / VETO
----------------
  Reports to: ceo
  Vetoes: crypto_trade_proposal

STATUS
------
SKELETON — implementation lands in Week 9-10 of the bring-up roadmap.
The constitutional layer (risk/sentinel.py) must exist first.
"""
from __future__ import annotations

from agents._base import AgentSpec, AgentProtocol


SPEC = AgentSpec(
    name="vp_crypto",
    persona="Helix",
    model="claude-sonnet-4-6",
    tier="vp",
    reports_to="ceo",
    can_veto=("crypto_trade_proposal",),
    system_prompt_path="agents/prompts/vp_crypto.txt",
)


class Agent(AgentProtocol):
    """Helix persona. See plan Section A.Line VP."""
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
