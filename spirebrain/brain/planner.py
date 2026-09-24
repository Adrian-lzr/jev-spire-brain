"""State summarisation and provider-independent strategic planning."""

from __future__ import annotations

import json
import time
from typing import Any

from .memory import RunMemory
from .protocol import BrainResponse, ActionCandidate, StrategicPlan
from spirebrain.strategy import strategy_context
from spirebrain.driver.decision_state import state_id as semantic_state_id


def stable_state_id(game: dict) -> str:
    """Compatibility wrapper around the shared semantic state identity."""
    return semantic_state_id(game)


def compact_state(game: dict) -> dict:
    """Keep the strategic prompt useful without copying unbounded live data."""
    if not isinstance(game, dict):
        return {}
    out = dict(game)
    for key in ("deck", "relics", "potions", "history", "events", "actions"):
        values = out.get(key)
        if isinstance(values, list):
            out[key] = values[-20:] if key in {"history", "events", "actions"} else values[:60]
    combat = out.get("combat") or out.get("combat_state")
    if isinstance(combat, dict):
        combat = dict(combat)
        if isinstance(combat.get("hand"), list):
            combat["hand"] = combat["hand"][:15]
        if isinstance(combat.get("monsters"), list):
            combat["monsters"] = combat["monsters"][:8]
        out["combat"] = combat
        out.pop("combat_state", None)
    screen_state = out.get("screen_state")
    if isinstance(screen_state, dict):
        screen_state = dict(screen_state)
        for key in ("cards", "relics", "potions", "options", "rest_options", "next_nodes"):
            values = screen_state.get(key)
            if isinstance(values, list):
                screen_state[key] = values[:20]
        out["screen_state"] = screen_state
    return out


class StrategicPlanner:
    """Build one bounded provider request and validate its response."""

    def __init__(self, provider, *, max_plan_steps: int = 5,
                 memory_events: int = 20) -> None:
        self.provider = provider
        self.max_plan_steps = max(2, min(5, int(max_plan_steps)))
        self.memory_events = max(1, int(memory_events))

    def plan(self, game: dict, candidates: list[ActionCandidate], *,
              guide_rules: list[dict] | None = None, trigger: str = "state",
              memory: RunMemory | None = None,
              previous: StrategicPlan | None = None,
              generation: int = 0) -> BrainResponse:
        state_id = stable_state_id(game)
        run_id = memory.run_id if memory else "local"
        if memory is not None:
            memory.observe(game, state_id=state_id)
        payload = {
            "state_id": state_id,
            "run_id": run_id,
            # The request ID is included even before a new plan exists.  This
            # lets logs and providers correlate a timeout with the exact state
            # that requested it without treating it as an executable command.
            "plan_id": previous.plan_id if previous else f"request-{state_id[:12]}",
            "generation": int(generation),
            "trigger": trigger,
            "max_plan_steps": self.max_plan_steps,
            "state": compact_state(game),
            "strategy_context": strategy_context(game),
            "memory": memory.digest(self.memory_events) if memory else {},
            "guide_rules": list(guide_rules or [])[:20],
            "candidates": [candidate.model_dict() for candidate in candidates if candidate.legal],
            "previous_plan": previous.model_dict() if previous else None,
            "generated_at": time.time(),
        }
        try:
            result = self.provider.plan(payload)
        except Exception as exc:  # provider failures are a side-channel fallback
            return BrainResponse(backend=getattr(self.provider, "backend_name", "unknown"),
                                 error=f"战略大脑异常：{type(exc).__name__}: {exc}", fallback=True)
        if not isinstance(result, BrainResponse):
            return BrainResponse(backend=getattr(self.provider, "backend_name", "unknown"),
                                 error="战略大脑返回了无效响应", fallback=True)
        if result.plan is not None:
            if result.plan.state_id != state_id or result.plan.run_id != run_id:
                return BrainResponse(
                    backend=result.backend, model=result.model,
                    latency_ms=result.latency_ms, request_id=result.request_id,
                    usage=result.usage, error="战略计划与当前状态不匹配", fallback=True,
                )
            legal_ids = {candidate.candidate_id for candidate in candidates if candidate.legal}
            referenced = (set(result.plan.preferred_candidates)
                          | set(result.plan.avoid_candidates))
            unknown = sorted(referenced - legal_ids)
            if unknown:
                return BrainResponse(
                    backend=result.backend, model=result.model,
                    latency_ms=result.latency_ms, request_id=result.request_id,
                    usage=result.usage,
                    error=f"战略计划引用了当前不存在的候选：{', '.join(unknown[:5])}",
                    fallback=True,
                )
            result.plan.bind_candidates(candidates)
            if memory is not None:
                memory.set_plan(result.plan)
        return result
