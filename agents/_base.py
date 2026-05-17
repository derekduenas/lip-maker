"""Shared persona base class and protocol interfaces.

All innait agents conform to this shape. The constitutional layer
(risk/sentinel.py — Week 3-4) enforces that only Approved proposals
reach order placement; this base class defines the proposal shape.

NOT YET IMPORTED BY ANY HOT PATH. This file ships in Week 1-2 as a
contract definition. Wiring lands in Weeks 5-12.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional


Severity = Literal["info", "warn", "critical"]
Decision = Literal["approve", "reject", "defer"]


@dataclass
class AgentSpec:
    """Static per-agent definition. Populated as a module-level CONFIG in
    each agent module. Read by `supervisor.py` to build the LangGraph."""
    name: str                       # short id, e.g. "ceo", "vp_lip"
    persona: str                    # human label, e.g. "Innait-Prime"
    model: str                      # "claude-opus-4-7" | "claude-sonnet-4-6" | "claude-haiku-4-5"
    tier: Literal["executive", "vp", "analyst", "research", "support"]
    reports_to: Optional[str]       # agent name; None for CEO
    can_veto: tuple[str, ...] = ()  # what kinds of decisions this agent vetoes
    system_prompt_path: str = ""    # path to the constitutional system prompt


@dataclass
class TradeProposal:
    """A VP's proposal for the CRO to evaluate. The CRO calls
    risk/sentinel.py:approve(proposal) which is the only path to order
    placement."""
    silo: str                       # 'lip' | 'macro' | 'crypto'
    venue: str                      # 'kalshi' | 'cme' | 'kraken' | 'ice' | 'pm_us'
    instrument: str                 # ticker / symbol
    side: Literal["yes", "no", "buy", "sell"]
    size: int                       # contracts / units
    price_cents: Optional[int]      # limit price; None = market
    rationale: str                  # human-readable why
    net_exposure_delta: dict[str, float] = field(default_factory=dict)
    # The vector used by risk/netting.py for cross-silo inventory netting,
    # e.g. {"USD": -1234.5, "BTC": 0.3, "ZQ_DV01": 100}
    proposed_by: str = ""           # agent name
    proposed_ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RiskDecision:
    """The CRO's verdict on a TradeProposal."""
    proposal: TradeProposal
    decision: Decision
    reason: str
    constitutional_checks_passed: dict[str, bool] = field(default_factory=dict)
    decided_ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Signal:
    """An analyst's contribution to signals_log."""
    signal_type: str                # 'vpin' | 'microprice_tilt' | 'fed_curve' | 'funding_rate' | 'regime'
    asset: str
    value: float | str | dict
    confidence: float = 0.0         # 0..1
    cross_silo_relevant: bool = False
    produced_by: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Postmortem:
    """A structured loss / veto post-mortem. Per the plan Section N.2,
    consumed by the RAG retrieval system to inform future decisions."""
    silo: str
    root_cause: str
    decision_sequence: list[str]
    loss_pnl_usd: float
    lesson: str
    lesson_category: str            # e.g. 'information_risk' | 'execution' | 'sizing'
    decay_half_life_days: int = 180
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    severity: Severity = "warn"


class AgentProtocol:
    """Minimal interface every agent implements. Real implementation
    lands when LangGraph wiring ships in Week 11-12."""

    SPEC: AgentSpec  # subclass overrides

    def read_inputs(self, blackboard) -> dict[str, Any]:
        """Pull what this agent needs from the blackboard (signals,
        recent postmortems, risk decisions, etc.). Implementations
        must be pure-read; no writes here."""
        raise NotImplementedError

    def propose(self, inputs: dict[str, Any]) -> Optional[TradeProposal | RiskDecision | Signal]:
        """Run the persona's reasoning. Returns a typed output for the
        next stage to consume. May return None to signal 'no action'."""
        raise NotImplementedError

    def report(self, output: Any, blackboard) -> None:
        """Write output to the blackboard. The supervisor calls this
        last; constitutional layer in risk/sentinel.py enforces that
        TradeProposals can only be persisted via approved RiskDecisions."""
        raise NotImplementedError
