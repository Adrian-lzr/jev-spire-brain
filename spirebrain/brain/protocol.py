"""Typed contracts shared by the GPT planner and the local action layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class PlanValidationError(ValueError):
    """Raised when a strategic model returns an unsafe or incomplete plan."""


@dataclass
class ActionCandidate:
    """One legal action that the local executor can turn into a command."""

    candidate_id: str
    kind: str
    label: str
    command: dict = field(default_factory=dict)
    legal: bool = True
    cost: Any = None
    target: Any = None
    goal_tags: list[str] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)
    uncertainty: str = ""
    description: str = ""
    # Kept as an explicit field for integrations that want to inspect the
    # executor template.  It is deliberately omitted from ``model_dict`` so
    # the strategic model can only select an ID, never author a command.
    command_template: dict | str | None = None

    def __post_init__(self) -> None:
        if not self.command and isinstance(self.command_template, dict):
            self.command = dict(self.command_template)
        if self.command_template is None:
            self.command_template = dict(self.command)

    def model_dict(self) -> dict:
        """The model-facing shape; protocol details stay out of the prompt."""
        out = {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "label": self.label,
            "legal": bool(self.legal),
            "goal_tags": list(self.goal_tags),
            "risk_tags": list(self.risk_tags),
        }
        if self.cost is not None:
            out["cost"] = self.cost
        if self.target is not None:
            out["target"] = self.target
        if self.description:
            out["description"] = self.description
        if self.uncertainty:
            out["uncertainty"] = self.uncertainty
        return out

    def to_dict(self) -> dict:
        out = self.model_dict()
        out["command"] = dict(self.command)
        out["command_template"] = self.command_template
        return out


@dataclass
class StrategicPlan:
    """A short, state-bound plan returned by the strategic model."""

    plan_id: str
    state_id: str
    run_id: str
    current_objective: str = ""
    long_term_goal: str = ""
    priority: list[str] = field(default_factory=list)
    preferred_candidates: list[str] = field(default_factory=list)
    avoid_candidates: list[str] = field(default_factory=list)
    resource_constraints: dict = field(default_factory=dict)
    next_steps: list[str] = field(default_factory=list)
    replan_triggers: list[str] = field(default_factory=list)
    reason: str = ""
    uncertainty: str = ""
    expires_after: int | None = None
    created_at: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict, *, state_id: str, run_id: str,
                  max_steps: int = 5) -> "StrategicPlan":
        if not isinstance(data, dict):
            raise PlanValidationError("战略计划必须是 JSON 对象")

        allowed = {
            "plan_id", "state_id", "run_id", "current_objective",
            "long_term_goal", "priority", "preferred_candidates",
            "avoid_candidates", "resource_constraints", "next_steps",
            "replan_triggers", "reason", "uncertainty", "expires_after",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            # In particular, reject a provider trying to smuggle a raw
            # CommunicationMod command into a strategic response.
            raise PlanValidationError(f"计划包含未允许字段：{', '.join(unknown[:5])}")
        required = {
            "plan_id", "state_id", "run_id", "current_objective", "long_term_goal",
            "priority", "preferred_candidates", "avoid_candidates",
            "resource_constraints", "next_steps", "replan_triggers", "reason",
            "uncertainty", "expires_after",
        }
        missing = sorted(required - set(data))
        if missing:
            raise PlanValidationError(f"计划缺少字段：{', '.join(missing[:5])}")

        def text(name: str, default: str = "", limit: int = 500) -> str:
            value = data.get(name, default)
            if value is None:
                return default
            if not isinstance(value, (str, int, float, bool)):
                raise PlanValidationError(f"{name} 必须是文本")
            value = str(value).strip()
            if len(value) > limit:
                raise PlanValidationError(f"{name} 超过长度限制")
            return value

        def strings(name: str, limit: int | None = None) -> list[str]:
            value = data.get(name, [])
            if value is None:
                return []
            if isinstance(value, str) or not isinstance(value, list):
                raise PlanValidationError(f"{name} 必须是字符串数组")
            out = []
            for item in value:
                if not isinstance(item, (str, int, float)):
                    raise PlanValidationError(f"{name} 包含无效项目")
                item = str(item).strip()
                if len(item) > 240:
                    raise PlanValidationError(f"{name} 包含过长项目")
                if item and item not in out:
                    out.append(item)
            return out[:limit] if limit is not None else out

        plan_id = text("plan_id", limit=120)
        if not plan_id:
            raise PlanValidationError("缺少 plan_id")
        # A plan is state-bound.  The model may echo the ID, but the caller's
        # fingerprint remains authoritative and is used when it is omitted.
        returned_state = text("state_id", state_id)
        if returned_state and returned_state != state_id:
            raise PlanValidationError("plan 的 state_id 已过期")
        returned_run = text("run_id", run_id)
        if returned_run and returned_run != run_id:
            raise PlanValidationError("plan 的 run_id 不匹配")

        expires = data.get("expires_after")
        if expires is not None:
            try:
                expires = max(1, min(5, int(expires)))
            except (TypeError, ValueError):
                raise PlanValidationError("expires_after 必须是整数") from None

        constraints = data.get("resource_constraints", {})
        if constraints is None:
            constraints = {}
        if not isinstance(constraints, dict):
            raise PlanValidationError("resource_constraints 必须是对象")

        return cls(
            plan_id=plan_id,
            state_id=state_id,
            run_id=run_id,
            current_objective=text("current_objective"),
            long_term_goal=text("long_term_goal"),
            priority=strings("priority", 8),
            preferred_candidates=strings("preferred_candidates", 8),
            avoid_candidates=strings("avoid_candidates", 12),
            resource_constraints=dict(constraints),
            next_steps=strings("next_steps", max_steps),
            replan_triggers=strings("replan_triggers", 12),
            reason=text("reason")[:500],
            uncertainty=text("uncertainty")[:300],
            expires_after=expires,
            raw=dict(data),
        )

    def model_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "state_id": self.state_id,
            "run_id": self.run_id,
            "current_objective": self.current_objective,
            "long_term_goal": self.long_term_goal,
            "priority": list(self.priority),
            "preferred_candidates": list(self.preferred_candidates),
            "avoid_candidates": list(self.avoid_candidates),
            "resource_constraints": dict(self.resource_constraints),
            "next_steps": list(self.next_steps),
            "replan_triggers": list(self.replan_triggers),
            "reason": self.reason,
            "uncertainty": self.uncertainty,
            "expires_after": self.expires_after,
        }


@dataclass
class BrainResponse:
    """A model result or a non-fatal fallback explanation."""

    plan: StrategicPlan | None = None
    backend: str = "unavailable"
    model: str = ""
    latency_ms: int = 0
    request_id: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""
    fallback: bool = False


@dataclass(frozen=True)
class StateSnapshot:
    """Bounded, versioned state envelope shared by all decision layers.

    The live CommunicationMod payload remains the source of truth.  This
    envelope gives providers and replay tools a stable place for the IDs that
    protect against late asynchronous results without forcing every caller to
    depend on a concrete game-state class.
    """

    state_id: str
    run_id: str
    screen_type: str = ""
    generation: int = 0
    payload: dict = field(default_factory=dict, repr=False)

    def model_dict(self) -> dict:
        return {
            "state_id": self.state_id,
            "run_id": self.run_id,
            "screen_type": self.screen_type,
            "generation": self.generation,
            "state": dict(self.payload),
        }


@dataclass
class ExecutionDecision:
    """The command chosen after GPT, JEV and legality have been reconciled."""

    primary_candidate: ActionCandidate | None = None
    alternative_candidate: ActionCandidate | None = None
    strategic_goal: str = ""
    reason: str = ""
    source_type: str = "rule_fallback"
    state_id: str = ""
    plan_id: str = ""
    jev_confidence: float = 0.0
    uncertain: bool = False
    candidates: list[dict] = field(default_factory=list)

    def detail(self) -> dict:
        primary = self.primary_candidate
        alternative = self.alternative_candidate
        return {
            "strategic_goal": self.strategic_goal,
            "plan_id": self.plan_id,
            "brain_source": self.source_type,
            "jev_confidence": self.jev_confidence,
            "alternative_command": dict(alternative.command) if alternative else None,
            "alternative_label": alternative.label if alternative else "",
            "alternative_reason": (
                f"如果当前目标不可行，可改为：{alternative.label}。"
                if alternative else ""
            ),
            "alternative_condition": (
                "当前建议不可执行、目标状态发生变化或需要保留资源时切换。"
                if alternative else ""
            ),
            "uncertain": bool(self.uncertain),
            "candidates": list(self.candidates),
            "candidate_id": primary.candidate_id if primary else "",
        }


class StrategicBrain(Protocol):
    """Provider interface implemented by OpenAI, mock and future backends."""

    backend_name: str

    def plan(self, payload: dict) -> BrainResponse:
        ...
