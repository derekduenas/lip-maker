"""innait — AI-native quant firm agent roster.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md

This package is the AGENT TIER of the architecture. The constitutional
layer (risk/sentinel.py, config/constitution.py), blackboard (data/innait.db),
and walk-forward harness (research/) sit underneath. LangGraph orchestrates
the agents via agents/supervisor.py.

ROSTER (14 agents, see plan Section A):

    Executive tier (Opus / Sonnet, high-context)
        CEO        — Innait-Prime    agents/ceo.py
        CFO        — Ledger          agents/cfo.py
        CRO        — Sentinel        agents/cro.py        (unconditional veto)

    Line VP tier (Sonnet)
        VP-LIP     — Harvester       agents/vp_lip.py
        VP-Macro   — Compass         agents/vp_macro.py
        VP-Crypto  — Helix           agents/vp_crypto.py

    Analyst tier (Haiku, parallel, structured-JSON)
        Tick                          agents/analyst_tick.py
        Drift                         agents/analyst_drift.py
        Basis                         agents/analyst_basis.py
        Weather                       agents/analyst_weather.py

    Research tier (Sonnet / Haiku)
        Quant Researcher — Lens       agents/quant_researcher.py
        Research Scout   — Recon      agents/research_scout.py

    Support tier (Sonnet)
        Ops        — Concierge       agents/ops.py
        Compliance — Counsel         agents/compliance.py

Every agent module declares:
  - PERSONA (the system prompt)
  - REPORTING_LINE (which agent receives this one's output)
  - VETO_SCOPE (what this agent can refuse)
  - read_inputs() / propose() / report() interface

These are SKELETONS today. Implementation lands in roadmap Weeks 5-12
per the plan. The constitutional layer (Weeks 3-4) must exist before
any of these gain order-placement authority.
"""

__all__ = [
    "ceo", "cfo", "cro",
    "vp_lip", "vp_macro", "vp_crypto",
    "analyst_tick", "analyst_drift", "analyst_basis", "analyst_weather",
    "quant_researcher", "research_scout",
    "ops", "compliance",
    "supervisor",
]
