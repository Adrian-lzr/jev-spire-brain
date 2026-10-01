"""State summarisation and provider-independent strategic planning."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .memory import RunMemory, RunMemorySnapshot
from .protocol import BrainResponse, ActionCandidate, StrategicPlan
from spirebrain.strategy import strategy_context
from spirebrain.knowledge.rag import retrieve_strategy_context
from spirebrain.driver.decision_state import state_id as semantic_state_id
from spirebrain.runtime_budget import DecisionBudget, use_budget


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
                 memory_events: int = 20, knowledge_enabled: bool = True) -> None:
        self.provider = provider
        self.max_plan_steps = max(2, min(5, int(max_plan_steps)))
        self.memory_events = max(1, int(memory_events))
        self.knowledge_enabled = bool(knowledge_enabled)

    def plan(self, game: dict, candidates: list[ActionCandidate], *,
              guide_rules: list[dict] | None = None, trigger: str = "state",
              memory: RunMemory | RunMemorySnapshot | None = None,
              previous: StrategicPlan | None = None,
              generation: int = 0,
              budget: DecisionBudget | None = None,
              decision_id: str = "",
              internal_timing: dict | None = None) -> BrainResponse:
        state_id = stable_state_id(game)
        run_id = memory.run_id if memory else "local"
        # Memory is observed and committed by the owning game loop.  A planner
        # may receive either the live object on a synchronous call or the
        # immutable snapshot used by a worker, but it must never mutate either
        # while building a provider request.
        selected_strategy = strategy_context(game)
        strategy_fingerprint = hashlib.sha256(json.dumps(
            selected_strategy, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:16]
        scope_value = "|".join((
            str(game.get("character", game.get("character_class", "")) or ""),
            str(int(game.get("act", 0) or 0)),
            str(int(game.get("floor", game.get("floor_num", 0)) or 0)),
            str(game.get("screen_type", "") or "").upper(),
        ))
        strategic_scope_id = hashlib.sha256(scope_value.encode("utf-8")).hexdigest()[:16]
        knowledge_game = dict(game) if isinstance(game, dict) else {}
        knowledge_game["strategy_context"] = selected_strategy
        knowledge_context = (
            retrieve_strategy_context(knowledge_game)
            if self.knowledge_enabled else {
                "status": "disabled", "character": "IRONCLAD", "items": [],
                "citation_ids": [],
                "usage": "strategy knowledge retrieval disabled by configuration",
            }
        )
        payload = {
            "state_id": state_id,
            "run_id": run_id,
            "run_epoch": int(getattr(memory, "run_epoch", 0) or 0),
            "decision_id": str(decision_id or ""),
            # The request ID is included even before a new plan exists.  This
            # lets logs and providers correlate a timeout with the exact state
            # that requested it without treating it as an executable command.
            "plan_id": previous.plan_id if previous else f"request-{state_id[:12]}",
            "generation": int(generation),
            "trigger": trigger,
            "max_plan_steps": self.max_plan_steps,
            "state": compact_state(game),
            "strategy_context": selected_strategy,
            # Retrieved claims are bounded, source-cited soft evidence.  They
            # cannot author commands, candidate IDs or card facts.
            "knowledge_context": knowledge_context,
            "memory": (memory.digest(self.memory_events)
                       if memory and hasattr(memory, "digest")
                       else (dict(memory) if isinstance(memory, dict) else {})),
            "guide_rules": list(guide_rules or [])[:20],
            "candidates": [candidate.model_dict() for candidate in candidates if candidate.legal],
            "previous_plan": previous.model_dict() if previous else None,
            "generated_at": time.time(),
            "strategy_fingerprint": strategy_fingerprint,
            "strategic_scope_id": strategic_scope_id,
        }
        # Enforce the shared decision budget at the planner boundary as well
        # as inside HTTP adapters.  Scripted and local providers may not call
        # ``DecisionBudget.reserve`` themselves; a zero-call budget must still
        # be a hard guarantee for every provider implementation.
        if budget is not None:
            if budget.calls >= budget.max_calls:
                budget.exhausted = budget.exhausted or "max_model_calls"
                return BrainResponse(
                    backend=getattr(self.provider, "backend_name", "unknown"),
                    error="max_model_calls_before_start", error_kind="budget_exhausted",
                    fallback=True,
                )
            if budget.remaining_ms <= 0:
                budget.exhausted = budget.exhausted or "decision_deadline"
                return BrainResponse(
                    backend=getattr(self.provider, "backend_name", "unknown"),
                    error="decision_deadline_before_start", error_kind="budget_exhausted",
                    fallback=True,
                )
        try:
            timing = dict(internal_timing or {})
            worker_started = timing.get("worker_started_monotonic")
            if isinstance(worker_started, (int, float)):
                timing["pre_provider_ms"] = max(
                    0, int((time.monotonic() - float(worker_started)) * 1000))
            payload["_internal_timing"] = timing
            with use_budget(budget):
                result = self.provider.plan(payload)
        except Exception as exc:  # provider failures are a side-channel fallback
            return BrainResponse(backend=getattr(self.provider, "backend_name", "unknown"),
                                 error=f"战略大脑异常：{type(exc).__name__}: {exc}",
                                 error_kind="provider_exception", fallback=True)
        if not isinstance(result, BrainResponse):
            return BrainResponse(backend=getattr(self.provider, "backend_name", "unknown"),
                                 error="战略大脑返回了无效响应",
                                 error_kind="parse_or_validation", fallback=True)
        if result.plan is not None:
            if result.plan.state_id != state_id or result.plan.run_id != run_id:
                return BrainResponse(
                    backend=result.backend, model=result.model,
                    latency_ms=result.latency_ms, request_id=result.request_id,
                    usage=result.usage, error="战略计划与当前状态不匹配",
                    error_kind="plan_validation", fallback=True,
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
                    error_kind="plan_validation", fallback=True,
                )
            result.plan.bind_candidates(candidates)
        return result
