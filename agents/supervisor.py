"""LangGraph supervisor — orchestrates the 14-agent innait firm.

Reference: /root/.claude/plans/ok-now-that-w-peppy-pumpkin.md (Sections A, D, E)

PATTERN — hierarchical supervisor with blackboard state (TradingAgents
arXiv 2412.20138):

    CEO (Innait-Prime)
        ├── CFO (Ledger)
        │       └── Lens, Recon, Ops
        ├── CRO (Sentinel)            [UNCONDITIONAL VETO]
        │       └── Compliance
        ├── VP-LIP (Harvester)
        │       └── Tick
        ├── VP-Macro (Compass)
        │       └── Drift
        └── VP-Crypto (Helix)
                └── Basis

    Cross-silo: Weather analyst → all three VPs + CRO (the common-good signal)

DAILY RHYTHM (Section E):
    07:30 ET — Morning standup (CEO-chaired)
    12:30 ET — Midday signal exchange (VP-to-VP)
    16:30 ET — End-of-day reconciliation (CFO-chaired)
    20:00 ET — Overnight research (analysts parallel)

ORDER PLACEMENT FLOW:
    Analyst → signals_log
    VP reads signals → TradeProposal
    CRO calls risk/sentinel.py:approve(proposal)  [CONSTITUTIONAL ENFORCEMENT]
    If approve → execution_log → venue adapter
    If reject → postmortem_log entry; trade does not happen

STATUS
------
SKELETON. Full implementation lands in Weeks 11-12 of the bring-up roadmap.
The agent stubs (agents/ceo.py etc.) exist; the constitutional layer
(risk/sentinel.py — Week 3-4) must ship before this supervisor wires
any of them into a path that places real orders.

When implementation lands, this module will:
  - Import all 14 agents
  - Build a langgraph.graph.StateGraph with nodes per agent
  - Configure conditional edges per the reporting hierarchy
  - Wire blackboard reads/writes via langgraph.checkpoint.sqlite (Week 1-4)
    or langgraph.checkpoint.postgres (Week 5+ when TimescaleDB lands)
  - Expose `simulate_day(date)` for paper-mode + chaos testing
"""
from __future__ import annotations

# Roster — imported here once supervisor is wired (Week 11-12)
AGENT_MODULES = [
    "agents.ceo",
    "agents.cfo",
    "agents.cro",
    "agents.vp_lip",
    "agents.vp_macro",
    "agents.vp_crypto",
    "agents.analyst_tick",
    "agents.analyst_drift",
    "agents.analyst_basis",
    "agents.analyst_weather",
    "agents.quant_researcher",
    "agents.research_scout",
    "agents.ops",
    "agents.compliance",
]


REPORTING_TREE = {
    # name: (reports_to, [direct_reports])
    "ceo":               (None,        ["cfo", "cro", "vp_lip", "vp_macro", "vp_crypto", "research_scout"]),
    "cfo":               ("ceo",       ["quant_researcher", "ops"]),
    "cro":               ("ceo",       ["compliance"]),
    "vp_lip":            ("ceo",       ["analyst_tick"]),
    "vp_macro":          ("ceo",       ["analyst_drift"]),
    "vp_crypto":         ("ceo",       ["analyst_basis"]),
    "analyst_tick":      ("vp_lip",    []),
    "analyst_drift":     ("vp_macro",  []),
    "analyst_basis":     ("vp_crypto", []),
    "analyst_weather":   ("ceo",       []),     # cross-silo broadcaster
    "quant_researcher":  ("cfo",       []),
    "research_scout":    ("ceo",       []),
    "ops":               ("cfo",       []),
    "compliance":        ("cro",       []),
}


def build_graph():
    """Construct the LangGraph supervisor. Returns a compiled graph
    that supervisor.invoke(...) can run a cycle through.

    Wiring lands in Week 11-12. Until then this is a stub that imports
    the agent modules to verify they're well-formed.
    """
    # TODO Week 11-12: build the actual langgraph.StateGraph
    # from langgraph.graph import StateGraph, START, END
    # graph = StateGraph(BlackboardState)
    # for agent_name in REPORTING_TREE:
    #     graph.add_node(agent_name, ...)
    # ... wire edges per REPORTING_TREE ...
    # return graph.compile(checkpointer=...)
    raise NotImplementedError(
        "supervisor.build_graph lands Week 11-12. Constitutional layer "
        "(risk/sentinel.py) must ship first — Weeks 3-4."
    )


def simulate_day(date_iso: str):
    """Run a full day's agent cycle in paper-mode against historical data.
    Used by chaos engineering (Section O.1) + integration tests."""
    raise NotImplementedError("simulate_day lands Week 11-12")


def standup(time_window: str = "morning"):
    """Run a checkpoint (morning_standup / midday_exchange / eod_reconciliation /
    overnight_research). Each gathers the relevant agents and runs one
    structured exchange via the blackboard."""
    raise NotImplementedError("standup lands Week 11-12")


if __name__ == "__main__":
    # Smoke: confirm all agent modules import + report their SPECs.
    import importlib
    print(f"innait roster — {len(AGENT_MODULES)} agents\n")
    for mod_name in AGENT_MODULES:
        try:
            m = importlib.import_module(mod_name)
            spec = m.SPEC
            print(f"  ✓ {spec.name:<22s} {spec.persona:<16s} "
                  f"{spec.model:<24s} {spec.tier:<10s} → {spec.reports_to or 'human'}")
        except Exception as e:
            print(f"  ✗ {mod_name}: {e}")
    print("\nReporting tree:")
    for name, (boss, reports) in REPORTING_TREE.items():
        print(f"  {name:<22s} reports to {boss or '(human)':<16s} "
              f"manages [{', '.join(reports) if reports else '—'}]")
