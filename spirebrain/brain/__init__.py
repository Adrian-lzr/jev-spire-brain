"""Strategic brain and action-broker primitives.

The strategic layer is deliberately separate from CommunicationMod.  A model
may describe goals and select an already enumerated candidate, but it never
gets to invent a wire command.
"""

from .protocol import (
    ActionCandidate,
    BrainResponse,
    DecisionContext,
    DecisionProposal,
    ExecutionDecision,
    FinalDecision,
    ObservedAction,
    PlanValidationError,
    RunSession,
    RunSessionSnapshot,
    StateSnapshot,
    StrategicPlan,
)

__all__ = [
    "ActionCandidate",
    "BrainResponse",
    "DecisionContext",
    "DecisionProposal",
    "ExecutionDecision",
    "FinalDecision",
    "ObservedAction",
    "PlanValidationError",
    "RunSession",
    "RunSessionSnapshot",
    "StateSnapshot",
    "StrategicPlan",
]
