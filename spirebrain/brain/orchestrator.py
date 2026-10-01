"""Coordinates the strategic provider, bounded run memory and action broker."""

from __future__ import annotations

import json
import copy
import queue
import threading
import time

from .action_broker import build_action_candidates, reconcile
from .gpt_client import LoggingStrategicClient, get_strategic_brain
from .memory import RunMemory
from .planner import StrategicPlanner, stable_state_id
from .protocol import (
    BrainResponse, DecisionContext, DecisionRequest, DecisionResult,
    ExecutionDecision, RunSessionSnapshot, StrategicPlan,
    PhaseStrategySnapshot,
)
from spirebrain.driver.live_state import GameSnapshot
from spirebrain.runtime_budget import DecisionBudget


def _screen(game: dict) -> str:
    return str((game or {}).get("screen_type", "")).upper()


class StrategicOrchestrator:
    """The only object the router needs to know about the GPT layer."""

    def __init__(self, *, backend: str = "openai", model: str | None = None,
                 endpoint: str | None = None,
                 reasoning_effort: str | None = None,
                 api_key: str | None = None,
                 timeout_ms: int = 6000, max_plan_steps: int = 5,
                 memory_events: int = 20, max_output_tokens: int = 900,
                 client=None, log_dir=None, async_planning: bool | None = None,
                 knowledge_enabled: bool = True,
                 planning_enabled: bool = True,
                 plan_retry_cooldown_ms: int = 30_000,
                 clock=time.monotonic) -> None:
        provider = client if client is not None else get_strategic_brain(
            backend,
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            reasoning_effort=reasoning_effort,
            timeout_ms=timeout_ms,
            max_output_tokens=max_output_tokens,
        )
        self.provider = LoggingStrategicClient(provider, str(log_dir) + "/brain_calls.jsonl") \
            if log_dir else provider
        # Network-backed strategic calls must never hold up the CommunicationMod
        # loop.  Local mock/disabled providers stay synchronous so offline demos
        # and deterministic tests can inspect a plan in the same call.
        inferred_async = getattr(
            self.provider, "async_required",
            str(getattr(self.provider, "backend_name", backend)).lower() in {"openai", "gpt"},
        )
        self.async_planning = bool(inferred_async if async_planning is None else async_planning)
        self.planning_enabled = bool(planning_enabled)
        self.memory = RunMemory(max_events=memory_events)
        self.planner = StrategicPlanner(self.provider, max_plan_steps=max_plan_steps,
                                        memory_events=memory_events,
                                        knowledge_enabled=knowledge_enabled)
        self.current_plan: StrategicPlan | None = None
        # A late strategic answer may be too old to authorize a concrete
        # action, but its objective/resource constraints can remain useful for
        # the same run and act.  This read-only context is deliberately kept
        # separate from ``current_plan`` so arbitration never consumes stale
        # candidate preferences.
        self.context_plan: StrategicPlan | None = None
        self.context_plan_reason = ""
        self.context_plan_state_id = ""
        self.context_plan_retained = 0
        self.context_plan_act = 0
        self.context_plan_run_epoch = 0
        self._clock = clock
        self.plan_retry_cooldown_ms = max(0, int(plan_retry_cooldown_ms))
        self._last_attempt_stage_key = ""
        self._last_attempt_at: float | None = None
        self._last_attempt_trigger = ""
        self.plan_attempts = 0
        self.plan_attempts_suppressed = 0
        self._advisor_plan_generation = -1
        self.last_response = BrainResponse(backend=getattr(self.provider, "backend_name", "unknown"))
        self.last_state_id = ""
        self.last_trigger_key = ""
        self.last_screen = ""
        self.plan_uses = 0
        self.plan_valid = False
        self.generation = 0
        self.last_hp_snapshot: int | None = None
        self.last_gold_snapshot: int | None = None
        self.last_deck_snapshot: int | None = None
        self.last_deck_signature: tuple = ()
        self.last_inventory_snapshot: tuple = ()
        self.last_enemy_snapshot: tuple = ()
        self._plan_lock = threading.RLock()
        self._plan_running = False
        self._plan_thread: threading.Thread | None = None
        self._closed = False
        self._queued_plan = None
        # Only the latest completed plan proposal can be useful.  Keeping an
        # unbounded result queue here lets a slow game loop accumulate results
        # that are already stale, so producer replacement is explicitly
        # latest-wins too.
        self._plan_results: queue.Queue = queue.Queue(maxsize=1)
        self.dropped_plan_results = 0
        self.expired_plan_results = 0
        self.expired_plan_result_reasons: dict[str, int] = {}
        self._pending_key: tuple[str, int, str, int] | None = None
        self._pending_request: DecisionRequest | None = None
        self._pending_state_id = ""
        self._pending_run_id = ""
        self._plan_screen = ""
        self._plan_act = 0
        self._plan_floor = 0
        # A strategic request is scoped to a scene/act/floor. Combat hand and
        # HP churn belongs to the tactical layer and must not supersede an
        # in-flight provider request on every poll.
        self._pending_scope: tuple[str, int, int] | None = None
        self.invalidated_plan_requests = 0
        self.plan_invalidation_reasons: dict[str, int] = {}
        self.event_sink = None

    @property
    def backend_name(self) -> str:
        return str(getattr(self.provider, "backend_name", "unknown"))

    def reset(self, run_id: str | None = None) -> None:
        # Keep the new run identity when the owning loop is resetting a plan;
        # dropping it would make terminal/result events look orphaned.
        self.memory.reset("" if run_id is None else str(run_id))
        self.current_plan = None
        self.context_plan = None
        self.context_plan_reason = ""
        self.context_plan_state_id = ""
        self.context_plan_retained = 0
        self.context_plan_act = 0
        self.context_plan_run_epoch = self.memory.run_epoch
        self._last_attempt_stage_key = ""
        self._last_attempt_at = None
        self._last_attempt_trigger = ""
        self._advisor_plan_generation = -1
        self.last_response = BrainResponse(backend=self.backend_name)
        self.last_state_id = ""
        self.last_trigger_key = ""
        self.last_screen = ""
        self.plan_uses = 0
        self.plan_valid = False
        self.generation += 1
        self.last_hp_snapshot = None
        self.last_gold_snapshot = None
        self.last_deck_snapshot = None
        self.last_deck_signature = ()
        self.last_inventory_snapshot = ()
        self.last_enemy_snapshot = ()
        queued = None
        with self._plan_lock:
            # A running request cannot be cancelled portably.  Advancing
            # generation makes its eventual result harmless; queued work can be
            # dropped immediately so a new run starts cleanly.
            queued = self._queued_plan
            self._queued_plan = None
            self._pending_key = None
            self._pending_request = None
            self._pending_state_id = ""
            self._pending_run_id = ""
            self._plan_screen = ""
            self._plan_act = 0
            self._plan_floor = 0
            self._pending_scope = None
        if queued is not None:
            self._record_skipped_plan(queued, "run_reset_before_start")

    def request_replan(self, reason: str = "player_deviation") -> None:
        """Invalidate the cached plan without discarding run memory."""
        self.plan_valid = False
        self.plan_uses = 0
        self.last_trigger_key = ""
        self._last_attempt_stage_key = ""
        self._last_attempt_at = None
        self._last_attempt_trigger = ""
        self.generation += 1
        queued = None
        with self._plan_lock:
            queued = self._queued_plan
            self._queued_plan = None
            self._pending_key = None
            self._pending_request = None
            self._pending_state_id = ""
            self._pending_run_id = ""
            self._pending_scope = None
        if queued is not None:
            self._record_skipped_plan(queued, f"{reason}_before_start")

    def close(self, timeout_ms: int = 100) -> bool:
        """Invalidate pending work and wait only a bounded time for its worker."""
        with self._plan_lock:
            self._closed = True
            self.generation += 1
            thread = self._plan_thread
        self._invalidate_pending_request("shutdown")
        with self._plan_lock:
            self._queued_plan = None
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(max(0, min(250, int(timeout_ms))) / 1000.0)
        return not thread.is_alive()

    @staticmethod
    def _rebind_plan(plan: StrategicPlan, *, game: dict, candidates,
                     state_id: str) -> StrategicPlan:
        """Move strategic context to the current state without moving actions.

        A network response may legitimately arrive after combat HP/hand/intent
        changed.  The objective and resource constraints remain useful within
        the same strategic scope, but positional candidate IDs do not.  Keep
        only preferences whose generation-time signatures still match, then
        bind the surviving candidates to the current snapshot.
        """
        rebound = copy.deepcopy(plan)
        if rebound.bindings_bound:
            current = {c.candidate_id: c.candidate_signature
                       for c in candidates if c.legal}
            rebound.preferred_candidates = [
                candidate_id for candidate_id in rebound.preferred_candidates
                if rebound.candidate_bindings.get(candidate_id) == current.get(candidate_id)
            ]
            rebound.avoid_candidates = [
                candidate_id for candidate_id in rebound.avoid_candidates
                if rebound.avoid_bindings.get(candidate_id) == current.get(candidate_id)
            ]
        else:
            # Legacy/in-memory plans without provenance may still carry
            # positional IDs. Preserve the strategy, never those references.
            rebound.preferred_candidates = []
            rebound.avoid_candidates = []
        rebound.state_id = state_id
        rebound.bind_candidates(list(candidates))
        return rebound

    def _drain_plan_results(self, game: dict | None = None,
                            candidates=None, state_id: str | None = None) -> None:
        """Apply a current-run result, rebinding actions after combat churn.

        Exact state matching is still required for a concrete action.  The
        strategic objective may cross ordinary combat polling changes when the
        run, generation and scene/act/floor scope are unchanged.
        """
        while True:
            try:
                result = self._plan_results.get_nowait()
            except queue.Empty:
                return
            if not isinstance(result, DecisionResult):
                self._record_expired_plan_result("invalid_result")
                continue
            if result.request is None:
                self._record_expired_plan_result("missing_request")
                continue
            request = result.request
            context = request.context
            result_state_id = context.state_id
            result_run_id = context.run_id
            result_run_epoch = context.run_epoch
            generation = context.generation
            previous = request.plan
            response = result.response or BrainResponse(
                backend=self.backend_name, error=result.error or "empty_plan_result",
                fallback=True)
            current_game = game or {}
            current_state_id = state_id or stable_state_id(current_game)
            current_scope = self._strategic_scope(current_game) if game is not None else None
            with self._plan_lock:
                exact = (self._pending_request is request
                         and self._pending_key == (result_state_id, generation,
                                                   result_run_id, result_run_epoch))
            same_scope = (current_scope is None
                          or self._strategic_scope(request.state) == current_scope)
            # Strategic scope may allow reuse across tactical state changes,
            # but only the exact latest request may commit its result.
            discard_reason = ""
            if not exact:
                discard_reason = "superseded_request"
            elif not same_scope:
                discard_reason = "scope_changed"
            elif generation != self.generation:
                discard_reason = "generation_changed"
            elif result_run_id != self.memory.run_id:
                discard_reason = "run_changed"
            elif result_run_epoch != self.memory.run_epoch:
                discard_reason = "run_epoch_changed"
            if discard_reason:
                # The player already changed screens/runs.  A late strategic
                # answer must never overwrite the current recommendation.
                if (response.plan is not None and game is not None
                        and result_run_id == self.memory.run_id
                        and result_run_epoch == self.memory.run_epoch
                        and self._context_scope_compatible(request.state, current_game)):
                    self._retain_context_plan(response.plan, current_game, discard_reason)
                self._record_expired_plan_result(discard_reason)
                self._emit_trace_event("expired_result", {
                    "run_id": result_run_id, "run_epoch": result_run_epoch,
                    "decision_id": context.decision_id or None,
                    "state_id": result_state_id,
                    "request_id": response.request_id or None,
                    "provider_request_ids": ([response.request_id]
                                              if response.request_id else []),
                    "plan_id": getattr(response.plan, "plan_id", "") or None,
                    "expired_result": True, "fallback_reason": discard_reason,
                    "generation": generation, "trigger": context.trigger,
                    "pending_state_id": self._pending_state_id or None,
                    "pending_generation": (self._pending_key[1]
                                            if self._pending_key else None),
                    "pending_run_id": self._pending_run_id or None,
                })
                continue
            with self._plan_lock:
                self._pending_key = None
                self._pending_request = None
                self._pending_state_id = ""
                self._pending_run_id = ""
                self._pending_scope = None
            self.last_response = response
            live_game = current_game if game is not None else request.state
            live_state_id = current_state_id if game is not None else result_state_id
            self.last_state_id = live_state_id
            self.last_trigger_key = self._trigger_key(live_game)
            self.last_screen = _screen(live_game)
            self.last_hp_snapshot = int(live_game.get("current_hp", live_game.get("hp", 0)) or 0)
            self.last_gold_snapshot = int(live_game.get("gold", 0) or 0)
            self.last_deck_snapshot = len(live_game.get("deck") or [])
            self.last_deck_signature = self._deck_signature(live_game)
            self.last_inventory_snapshot = tuple(str(x) for x in (live_game.get("relics") or [])) + tuple(
                str(x) for x in (live_game.get("potions") or []))
            self.last_enemy_snapshot = self._combat_enemy_signature(live_game)
            self.plan_uses = 0
            if response.plan is not None:
                if game is not None and candidates is not None:
                    self.current_plan = self._rebind_plan(
                        response.plan, game=live_game, candidates=candidates,
                        state_id=live_state_id,
                    )
                else:
                    self.current_plan = response.plan
                self.plan_valid = True
                self._plan_screen = _screen(live_game)
                self._plan_act = int(live_game.get("act", 0) or 0)
                self._plan_floor = int(live_game.get("floor", live_game.get("floor_num", 0)) or 0)
                self.memory.set_plan(self.current_plan)
                self.context_plan = copy.deepcopy(self.current_plan)
                self.context_plan_reason = "current_plan"
                self.context_plan_state_id = live_state_id
                self.context_plan_act = int(live_game.get("act", 0) or 0)
                self.context_plan_run_epoch = self.memory.run_epoch
            elif previous is not None and self._can_reuse_plan(previous, live_game):
                # Keep a short cached plan through a transient timeout/invalid
                # JSON response.  Candidate reconciliation still filters any
                # item that disappeared from the live state.
                self.current_plan = previous
                self.plan_valid = True
                self.context_plan = copy.deepcopy(previous)
                self.context_plan_reason = "cached_plan"
                self.context_plan_state_id = live_state_id
                self.context_plan_act = int(live_game.get("act", 0) or 0)
                self.context_plan_run_epoch = self.memory.run_epoch
            else:
                self.plan_valid = False
            disposition = (
                "committed_plan" if response.plan is not None else
                "retained_previous" if self.plan_valid else "fallback"
            )
            self._record_plan_result(
                run_id=result_run_id, run_epoch=result_run_epoch,
                decision_id=context.decision_id, state_id=result_state_id,
                request_id=response.request_id,
                plan_id=getattr(response.plan, "plan_id", ""),
                disposition=disposition,
                commit_latency_ms=int((time.monotonic() - context.created_at) * 1000),
                reason=response.error if disposition == "fallback" else "",
            )

    def _can_reuse_plan(self, plan: StrategicPlan | None, game: dict) -> bool:
        if plan is None or plan.run_id != self.memory.run_id:
            return False
        screen = _screen(game)
        if self._plan_screen and screen != self._plan_screen:
            return False
        act = int(game.get("act", 0) or 0)
        floor = int(game.get("floor", game.get("floor_num", 0)) or 0)
        return (not self._plan_act or act == self._plan_act) and \
            (not self._plan_floor or floor == self._plan_floor)

    @staticmethod
    def _context_scope_compatible(previous: dict, current: dict) -> bool:
        """Allow a late objective to survive ordinary same-act navigation.

        A context plan never authorizes a command. Keeping it across a floor
        transition is still useful for the player's strategic target, but a
        different act can invalidate HP/resource assumptions and must clear it.
        """
        try:
            previous_act = int(previous.get("act", 0) or 0)
            current_act = int(current.get("act", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return False
        return previous_act > 0 and previous_act == current_act

    def _retain_context_plan(self, plan: StrategicPlan, game: dict,
                             reason: str) -> None:
        """Store only strategic meaning from a stale provider response."""
        retained = copy.deepcopy(plan)
        # Candidate IDs are bound to the old state. Remove them from the
        # context copy so a future caller cannot accidentally treat this as an
        # executable preference. The owner loop will request a fresh plan when
        # a strategic trigger requires concrete choices.
        retained.preferred_candidates = []
        retained.avoid_candidates = []
        retained.candidate_bindings = {}
        retained.avoid_bindings = {}
        retained.bindings_bound = False
        retained.state_id = stable_state_id(game)
        self.context_plan = retained
        self.context_plan_reason = str(reason or "stale_result")
        self.context_plan_state_id = retained.state_id
        self.context_plan_act = int(game.get("act", 0) or 0)
        self.context_plan_run_epoch = self.memory.run_epoch
        self.context_plan_retained += 1
        self._emit_trace_event("strategic_context_retained", {
            "run_id": self.memory.run_id or None,
            "run_epoch": self.memory.run_epoch,
            "decision_id": None,
            "state_id": retained.state_id,
            "plan_id": retained.plan_id or None,
            "fallback_reason": str(reason or "stale_result"),
            "context_only": True,
            "generation": self.generation,
            "act": self.context_plan_act,
        })

    def tactical_context_for(self, game: dict) -> dict | None:
        """Return a scoped strategic goal, never stale candidate preferences."""
        plan = self.context_plan
        game_run_id = str(game.get("run_id", game.get("runId", "")) or "")
        if (plan is None or plan.run_id != self.memory.run_id
                or (game_run_id and game_run_id != self.memory.run_id)
                or self.context_plan_run_epoch != self.memory.run_epoch
                or self.context_plan_act <= 0
                or int(game.get("act", 0) or 0) != self.context_plan_act):
            return None
        return {
            "plan_id": plan.plan_id,
            "goal": plan.current_objective,
            "resource_constraints": copy.deepcopy(plan.resource_constraints),
            "context_only": True,
        }

    def _usable_plan(self, game: dict) -> StrategicPlan | None:
        plan = self.current_plan if self.plan_valid else None
        return plan if self._can_reuse_plan(plan, game) else None

    def commit_worker_plan(self, plan: StrategicPlan | None, game: dict,
                           *, candidates=None,
                           advisor_generation: int | None = None) -> bool:
        """Commit a worker proposal on the owning game-loop boundary.

        Advice workers may calculate with a private clone, but they never own
        the live plan. The caller has already checked the task generation and
        state identity; this method performs the remaining run/scope check and
        rebinds candidate preferences to the current legal snapshot.
        """
        if not isinstance(plan, StrategicPlan) or not isinstance(game, dict):
            return False
        if plan.run_id != self.memory.run_id:
            return False
        if (advisor_generation is not None
                and advisor_generation < self._advisor_plan_generation):
            return False
        snapshot = GameSnapshot.from_communication(game)
        if plan.state_id not in {snapshot.state_id, stable_state_id(game)}:
            return False
        game = snapshot.payload
        current_candidates = build_action_candidates(game)
        plan = self._rebind_plan(
            plan, game=game, candidates=current_candidates,
            state_id=snapshot.state_id,
        )
        self.current_plan = plan
        self.plan_valid = True
        self._plan_screen = _screen(game)
        self._plan_act = int(game.get("act", 0) or 0)
        self._plan_floor = int(game.get("floor", game.get("floor_num", 0)) or 0)
        self.plan_uses = 0
        self.memory.set_plan(plan)
        self.last_state_id = stable_state_id(game)
        self.last_trigger_key = self._trigger_key(game)
        self.last_screen = _screen(game)
        self.last_hp_snapshot = int(game.get("current_hp", game.get("hp", 0)) or 0)
        self.last_gold_snapshot = int(game.get("gold", 0) or 0)
        self.last_deck_snapshot = len(game.get("deck") or [])
        self.last_deck_signature = self._deck_signature(game)
        self.last_inventory_snapshot = tuple(str(x) for x in (game.get("relics") or [])) + tuple(
            str(x) for x in (game.get("potions") or []))
        self.last_enemy_snapshot = self._combat_enemy_signature(game)
        self._record_plan_attempt(game, "advisor_plan_committed")
        if advisor_generation is not None:
            self._advisor_plan_generation = advisor_generation
        return True

    def commit_late_worker_plan(self, plan: StrategicPlan | None,
                                source_game: dict, current_game: dict, *,
                                run_id: str, run_epoch: int,
                                generation: int) -> bool:
        """Commit phase strategy from a stale advice task, never its action.

        A delayed worker's concrete action is invalid as soon as the state
        changes. Its strategic objective can survive ordinary tactical state
        changes only while the strategic stage key remains stable, and is
        rebound to the current legal candidates and candidate signatures.
        """
        if (not isinstance(plan, StrategicPlan)
                or not isinstance(source_game, dict)
                or not isinstance(current_game, dict)):
            return False
        if (str(run_id or "") != self.memory.run_id
                or int(run_epoch) != self.memory.run_epoch
                or plan.run_id != self.memory.run_id
                or generation < self._advisor_plan_generation):
            return False
        source = GameSnapshot.from_communication(source_game)
        current = GameSnapshot.from_communication(current_game)
        if plan.state_id != source.state_id:
            return False
        source_run_id = str(source.payload.get("run_id", source.payload.get("runId", "")) or "")
        current_run_id = str(current.payload.get("run_id", current.payload.get("runId", "")) or "")
        if ((source_run_id and source_run_id != run_id)
                or (current_run_id and current_run_id != run_id)
                or self._strategic_stage_key(source.payload)
                != self._strategic_stage_key(current.payload)):
            return False
        if self.plan_valid and self._usable_plan(current.payload) is not None:
            # A valid plan already committed for the current phase is newer
            # evidence than a delayed worker result.
            return False

        current_candidates = build_action_candidates(current.payload)
        rebound = self._rebind_plan(
            plan, game=current.payload, candidates=current_candidates,
            state_id=current.state_id,
        )
        dropped_preferences = (
            len(plan.preferred_candidates) - len(rebound.preferred_candidates)
            + len(plan.avoid_candidates) - len(rebound.avoid_candidates)
        )
        self.current_plan = rebound
        self.plan_valid = True
        self._plan_screen = current.screen_type
        self._plan_act = int(current.payload.get("act", 0) or 0)
        self._plan_floor = int(current.payload.get(
            "floor", current.payload.get("floor_num", 0)) or 0)
        self.plan_uses = 0
        self.memory.set_plan(rebound)
        self.context_plan = copy.deepcopy(rebound)
        self.context_plan_reason = "late_phase_plan_rebound"
        self.context_plan_state_id = current.state_id
        self.context_plan_act = self._plan_act
        self.context_plan_run_epoch = self.memory.run_epoch
        self.last_state_id = current.state_id
        self.last_trigger_key = self._trigger_key(current.payload)
        self.last_screen = current.screen_type
        self.last_hp_snapshot = int(current.payload.get("current_hp", current.payload.get("hp", 0)) or 0)
        self.last_gold_snapshot = int(current.payload.get("gold", 0) or 0)
        self.last_deck_snapshot = len(current.payload.get("deck") or [])
        self.last_deck_signature = self._deck_signature(current.payload)
        self.last_inventory_snapshot = tuple(str(x) for x in (current.payload.get("relics") or [])) + tuple(
            str(x) for x in (current.payload.get("potions") or []))
        self.last_enemy_snapshot = self._combat_enemy_signature(current.payload)
        self._record_plan_attempt(current.payload, "late_phase_plan_rebound")
        self._advisor_plan_generation = generation
        self._emit_trace_event("strategic_plan_rebound", {
            "run_id": run_id,
            "run_epoch": run_epoch,
            "decision_id": None,
            "source_state_id": source.state_id,
            "state_id": current.state_id,
            "plan_id": rebound.plan_id or None,
            "reason": "late_action_discarded_same_act_plan_rebound",
            "candidate_preferences_dropped": dropped_preferences,
            "generation": generation,
        })
        return True

    def _queue_plan(self, game: dict, candidates, guide_rules, trigger: str,
                    previous: StrategicPlan | None, state_id: str,
                    trigger_key: str, budget: DecisionBudget | None = None,
                    decision_id: str = "") -> None:
        """Queue one latest-wins strategic request and return immediately."""
        run_id = self.memory.run_id
        replaced_job = None
        with self._plan_lock:
            if self._closed:
                return
            run_epoch = self.memory.run_epoch
            key = (state_id, self.generation, run_id, run_epoch)
            if self._pending_key == key:
                return
            # Providers receive a deeply immutable snapshot. They can
            # summarize it, but cannot append facts or replace the live plan
            # while the game loop is processing a newer state.
            memory = self.memory.snapshot()
            try:
                game_copy = copy.deepcopy(dict(game))
            except Exception:
                game_copy = dict(game)
            try:
                candidate_copy = list(copy.deepcopy(candidates))
            except Exception:
                candidate_copy = list(candidates)
            context = DecisionContext(
                run=RunSessionSnapshot(run_id=run_id, run_epoch=run_epoch,
                                       mode="advise"),
                state_id=state_id, decision_id=str(decision_id or ""),
                generation=self.generation,
                trigger=trigger,
                budget_ms=(budget.remaining_ms if budget is not None else None),
                state=game_copy,
            )
            request = DecisionRequest(
                context=context, state=game_copy, memory=memory,
                candidates=tuple(candidate_copy), plan=previous,
                deadline_monotonic=(budget.deadline if budget is not None else None),
            )
            # Keep the latest-wins tuple small and explicit: the worker owns no
            # live session state and receives one immutable request snapshot.
            job = (request, list(guide_rules or []), trigger, previous,
                   trigger_key, budget)
            self._pending_key = key
            self._pending_request = request
            self._pending_state_id = state_id
            self._pending_run_id = run_id
            self._pending_scope = self._strategic_scope(game)
            replaced_job = self._queued_plan
            self._queued_plan = job
            self.last_response = BrainResponse(
                backend=self.backend_name, error="战略规划进行中", fallback=True)
            if not self._plan_running:
                self._plan_running = True
                self._plan_thread = threading.Thread(
                    target=self._plan_worker, daemon=True,
                    name="SpireBrainStrategic")
                self._plan_thread.start()
        if replaced_job is not None:
            self._record_skipped_plan(replaced_job, "superseded_before_start")

    def _emit_trace_event(self, event_type: str, fields: dict) -> None:
        sink = self.event_sink
        if callable(sink):
            try:
                sink(event_type, fields)
            except Exception:
                pass

    def _record_plan_result(self, *, run_id: str, run_epoch: int,
                            decision_id: str, state_id: str, request_id: str,
                            plan_id: str, disposition: str,
                            commit_latency_ms: int, reason: str = "") -> None:
        self._emit_trace_event("strategic_plan_commit", {
            "run_id": run_id or None,
            "run_epoch": int(run_epoch),
            "decision_id": decision_id or None,
            "state_id": state_id or None,
            "request_id": request_id or None,
            "plan_id": plan_id or None,
            "result_disposition": disposition,
            "commit_latency_ms": max(0, int(commit_latency_ms)),
            "fallback_reason": reason or None,
        })

    def _record_skipped_plan(self, job, reason: str) -> None:
        try:
            request = job[0]
            context = request.context
        except (TypeError, IndexError, AttributeError):
            return
        self._emit_trace_event("decision_skipped", {
            "run_id": context.run_id or None,
            "run_epoch": context.run_epoch,
            "decision_id": context.decision_id or None,
            "state_id": context.state_id or None,
            "reason": reason,
            "generation": context.generation,
        })

    def _plan_worker(self) -> None:
        while True:
            with self._plan_lock:
                job = self._queued_plan
                self._queued_plan = None
                if job is None:
                    self._plan_running = False
                    if self._plan_thread is threading.current_thread():
                        self._plan_thread = None
                    return
            request, guide_rules, trigger, previous, trigger_key, budget = job
            context = request.context
            game = request.state
            candidates = list(request.candidates)
            generation = context.generation
            state_id = context.state_id
            run_id = context.run_id
            run_epoch = context.run_epoch
            memory = request.memory
            decision_id = context.decision_id
            # The decision deadline starts when the request was created, not
            # when this single worker eventually gets around to it.  Do not
            # issue network work for a task that expired while queued.  Check
            # the call cap here as well as in providers so scripted/custom
            # providers cannot accidentally bypass max_model_calls=0.
            if budget is not None and budget.remaining_ms <= 0:
                response = BrainResponse(
                    backend=self.backend_name,
                    error="decision_deadline_before_start",
                    error_kind="budget_exhausted", fallback=True,
                )
            elif budget is not None and budget.calls >= budget.max_calls:
                response = BrainResponse(
                    backend=self.backend_name,
                    error="max_model_calls_before_start",
                    error_kind="budget_exhausted", fallback=True,
                )
            elif (budget is None and request.deadline_monotonic is not None
                  and time.monotonic() >= request.deadline_monotonic):
                response = BrainResponse(
                    backend=self.backend_name,
                    error="decision_deadline_before_start",
                    error_kind="budget_exhausted", fallback=True,
                )
            else:
                try:
                    worker_started = time.monotonic()
                    response = self.planner.plan(
                        game, candidates, guide_rules=guide_rules, trigger=trigger,
                        memory=memory, previous=previous, generation=generation,
                        budget=budget, decision_id=decision_id,
                        internal_timing={
                            "queued_monotonic": context.created_at,
                            "worker_started_monotonic": worker_started,
                            "queue_wait_ms": max(0, int(
                                (worker_started - context.created_at) * 1000)),
                        },
                    )
                except Exception as exc:  # provider failures are non-fatal
                    response = BrainResponse(
                        backend=self.backend_name,
                        error=f"战略大脑异常：{type(exc).__name__}: {exc}",
                        error_kind="provider_exception",
                        fallback=True,
                    )
            if response.fallback and response.error_kind == "plan_validation":
                self._emit_trace_event("plan_validation_rejected", {
                    "run_id": run_id or None,
                    "run_epoch": run_epoch,
                    "decision_id": decision_id or None,
                    "state_id": state_id,
                    "request_id": response.request_id or None,
                    "plan_id": getattr(response.plan, "plan_id", "") or None,
                    "error_kind": response.error_kind,
                    "fallback_reason": response.error or "plan_validation",
                    "generation": generation,
                    "trigger": trigger,
                })
            result = DecisionResult(request=request, response=response)
            self._enqueue_plan_result(result)

    def _enqueue_plan_result(self, result: DecisionResult) -> None:
        """Keep the completion slot bounded and terminate every displaced task."""
        try:
            self._plan_results.put_nowait(result)
            return
        except queue.Full:
            pass
        try:
            displaced = self._plan_results.get_nowait()
        except queue.Empty:
            displaced = None
        if displaced is not None:
            self.dropped_plan_results += 1
            self._record_expired_plan_result("completion_queue_replaced", displaced)
        try:
            self._plan_results.put_nowait(result)
        except queue.Full:
            self.dropped_plan_results += 1
            self._record_expired_plan_result("completion_queue_race", result)

    def _trigger_key(self, game: dict) -> str:
        screen = _screen(game)
        act = game.get("act", 0)
        floor = game.get("floor", game.get("floor_num", 0))
        if screen in {"SHOP", "SHOP_SCREEN"}:
            # A shop is one strategic phase even though its shelf and gold
            # change after each purchase. The existing objective/resource
            # policy should guide those follow-up choices; concrete item
            # preferences remain protected by candidate signatures.
            return f"{screen}:{act}:{floor}"
        # Strategic changes, unlike combat animation fields, should expire a
        # plan.  Gold/deck/HP are enough to catch purchases and major damage.
        gold = game.get("gold", 0)
        hp = game.get("current_hp", game.get("hp", 0))
        deck = len(game.get("deck") or [])
        # Combat intentionally ignores hand/intent churn here; the tactical
        # layer refreshes every step without paying for a new strategic plan.
        # Ordinary HP loss is also tactical noise while the player remains
        # above the danger threshold. A slow strategic response can still be
        # safely rebound to the current legal candidates when it completes.
        if screen == "COMBAT":
            inventory = tuple(str(x) for x in (game.get("relics") or [])) + tuple(
                str(x) for x in (game.get("potions") or []))
            return f"{screen}:{act}:{floor}:{gold}:{hp}:{deck}:{inventory}"
        # Rewards, events, rests and map routes can change without changing HP
        # or deck size, so their complete bounded screen state participates in
        # the trigger key.
        state = game.get("screen_state") or game.get("map") or {}
        try:
            state_key = json.dumps(state, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            state_key = repr(state)
        return f"{screen}:{act}:{floor}:{gold}:{hp}:{deck}:{state_key}"

    @staticmethod
    def _combat_enemy_signature(game: dict) -> tuple:
        combat = game.get("combat") or game.get("combat_state") or {}
        monsters = combat.get("monsters") or [] if isinstance(combat, dict) else []
        return tuple(
            (index,
             str(enemy.get("uuid", enemy.get("instance_id", enemy.get("id", "")))),
             str(enemy.get("name", "")),
             bool(enemy.get("is_gone", False)),
             bool(enemy.get("half_dead", False)))
            for index, enemy in enumerate(monsters) if isinstance(enemy, dict)
        )

    @staticmethod
    def _deck_signature(game: dict) -> tuple:
        """Represent deck composition without depending on serialization order."""
        signature = []
        for card in game.get("deck") or []:
            if isinstance(card, dict):
                signature.append((
                    str(card.get("id", card.get("name", ""))),
                    bool(card.get("upgraded", card.get("upgrades", 0))),
                ))
            else:
                signature.append((str(card), False))
        return tuple(sorted(signature))

    def _strategic_stage_key(self, game: dict) -> str:
        """Identify a strategic checkpoint, not an individual combat action."""
        screen = _screen(game)
        common = (self.memory.run_id, self.memory.run_epoch,
                  screen, int(game.get("act", 0) or 0),
                  int(game.get("floor", game.get("floor_num", 0)) or 0))
        if screen != "COMBAT":
            identity = common + (self._trigger_key(game),)
        else:
            combat = game.get("combat") or game.get("combat_state") or {}
            enemies = self._combat_enemy_signature(game)
            deck = self._deck_signature(game)
            inventory = (tuple(str(value) for value in (game.get("relics") or []))
                         + tuple(str(value) for value in (game.get("potions") or [])))
            player = combat.get("player", {}) if isinstance(combat, dict) else {}
            if not isinstance(player, dict):
                player = {}
            hp = int(game.get("current_hp", player.get("current_hp", 0)) or 0)
            max_hp = int(game.get("max_hp", player.get("max_hp", 0)) or 0)
            danger = bool(max_hp and hp / max_hp <= 0.30)
            identity = common + (enemies, deck, inventory,
                                 int(game.get("gold", 0) or 0), danger)
        return json.dumps(identity, ensure_ascii=False, default=str, separators=(",", ":"))

    def _retry_is_suppressed(self, game: dict, trigger: str | None) -> bool:
        stage_key = self._strategic_stage_key(game)
        force_triggers = {
            "run_start", "act_change", "screen_change", "major_change",
            "player_deviation", "combat_start",
        }
        forced = trigger in force_triggers
        # In advise/play loops combat_start can be repeated while the first
        # request is still pending because last_screen changes only on commit.
        # It is a trigger only when this encounter has not already been tried.
        if trigger == "combat_start" and stage_key == self._last_attempt_stage_key:
            forced = False
        if forced or stage_key != self._last_attempt_stage_key:
            return False
        if self._last_attempt_at is None:
            return False
        if (self._clock() - self._last_attempt_at) * 1000 >= self.plan_retry_cooldown_ms:
            return False
        self.plan_attempts_suppressed += 1
        return True

    def _record_plan_attempt(self, game: dict, trigger: str) -> None:
        self._last_attempt_stage_key = self._strategic_stage_key(game)
        self._last_attempt_at = self._clock()
        self._last_attempt_trigger = str(trigger or "state")
        self.plan_attempts += 1

    @staticmethod
    def _strategic_scope(game: dict) -> tuple[str, int, int]:
        return (
            _screen(game),
            int(game.get("act", 0) or 0),
            int(game.get("floor", game.get("floor_num", 0)) or 0),
        )

    @staticmethod
    def _combat_strategy_inputs_changed(previous: dict, current: dict) -> bool:
        """Detect strategic combat changes without treating each poll as new work."""
        def inventory(state: dict) -> tuple:
            return tuple(str(value) for value in (state.get("relics") or [])) + tuple(
                str(value) for value in (state.get("potions") or []))

        def enemy_group(state: dict) -> tuple:
            combat = state.get("combat") or state.get("combat_state") or {}
            monsters = combat.get("monsters") or [] if isinstance(combat, dict) else []
            return tuple(
                (index,
                 str(enemy.get("uuid", enemy.get("instance_id", enemy.get("id", "")))),
                 str(enemy.get("name", "")),
                 bool(enemy.get("is_gone", False)),
                 bool(enemy.get("half_dead", False)))
                for index, enemy in enumerate(monsters) if isinstance(enemy, dict)
            )

        if (int(previous.get("gold", 0) or 0) != int(current.get("gold", 0) or 0)
                or StrategicOrchestrator._deck_signature(previous)
                != StrategicOrchestrator._deck_signature(current)
                or inventory(previous) != inventory(current)
                or enemy_group(previous) != enemy_group(current)):
            return True

        old_combat = previous.get("combat") or previous.get("combat_state") or {}
        new_combat = current.get("combat") or current.get("combat_state") or {}
        old_player = old_combat.get("player", {}) if isinstance(old_combat, dict) else {}
        new_player = new_combat.get("player", {}) if isinstance(new_combat, dict) else {}
        old_hp = int(previous.get("current_hp", old_player.get("current_hp", 0)) or 0)
        new_hp = int(current.get("current_hp", new_player.get("current_hp", 0)) or 0)
        old_max = int(previous.get("max_hp", old_player.get("max_hp", 0)) or 0)
        new_max = int(current.get("max_hp", new_player.get("max_hp", 0)) or 0)
        if not old_max or not new_max:
            return False
        old_ratio = old_hp / old_max
        new_ratio = new_hp / new_max
        return old_ratio > 0.30 >= new_ratio

    def _pending_request_covers(self, game: dict, trigger: str | None) -> bool:
        """Return whether a pending strategic request still covers this state."""
        with self._plan_lock:
            request = self._pending_request
        if request is None:
            return False
        context = request.context
        if (context.run_id != self.memory.run_id
                or context.run_epoch != self.memory.run_epoch
                or self._strategic_scope(request.state) != self._strategic_scope(game)):
            return False
        if trigger in {"run_start", "act_change", "screen_change", "major_change",
                       "player_deviation", "combat_start"}:
            same_combat_start = (
                trigger == "combat_start"
                and context.trigger == "combat_start"
                and _screen(request.state) == "COMBAT"
                and _screen(game) == "COMBAT"
            )
            if not same_combat_start:
                return False
        if _screen(game) == "COMBAT":
            return not self._combat_strategy_inputs_changed(request.state, game)
        return self._trigger_key(request.state) == self._trigger_key(game)

    def _invalidate_pending_request(self, reason: str) -> None:
        queued = None
        with self._plan_lock:
            if self._pending_request is None:
                return
            self.invalidated_plan_requests += 1
            self.plan_invalidation_reasons[reason] = (
                self.plan_invalidation_reasons.get(reason, 0) + 1)
            self._pending_request = None
            self._pending_key = None
            self._pending_state_id = ""
            self._pending_run_id = ""
            self._pending_scope = None
            # Queued work has not started and may be replaced. Running provider
            # calls finish naturally; they no longer hold the commit identity.
            queued = self._queued_plan
            self._queued_plan = None
        if queued is not None:
            self._record_skipped_plan(queued, f"{reason}_before_start")

    def _record_expired_plan_result(self, reason: str,
                                    result: DecisionResult | None = None) -> None:
        self.expired_plan_results += 1
        self.expired_plan_result_reasons[reason] = (
            self.expired_plan_result_reasons.get(reason, 0) + 1)
        if result is None or result.request is None:
            return
        context = result.request.context
        response = result.response
        request_id = getattr(response, "request_id", "") or ""
        with self._plan_lock:
            pending_state_id = self._pending_state_id or None
            pending_generation = (self._pending_key[1]
                                  if self._pending_key else None)
            pending_run_id = self._pending_run_id or None
        self._emit_trace_event("expired_result", {
            "run_id": context.run_id or None,
            "run_epoch": context.run_epoch,
            "decision_id": context.decision_id or None,
            "state_id": context.state_id or None,
            "request_id": request_id or None,
            "provider_request_ids": [request_id] if request_id else [],
            "plan_id": getattr(getattr(response, "plan", None), "plan_id", "") or None,
            "expired_result": True, "fallback_reason": reason,
            "generation": context.generation, "trigger": context.trigger,
            "pending_state_id": pending_state_id,
            "pending_generation": pending_generation,
            "pending_run_id": pending_run_id,
        })

    def _needs_replan(self, game: dict, trigger: str | None = None) -> bool:
        if self.current_plan is None or not self.plan_valid:
            return not self._retry_is_suppressed(game, trigger)
        screen = _screen(game)
        # expires_after describes the short-plan horizon. It must not turn
        # every few tactical actions into another strategic network request.
        key = self._trigger_key(game)
        if key != self.last_trigger_key and screen != "COMBAT":
            return True
        if screen == "COMBAT":
            hp = int(game.get("current_hp", game.get("hp", 0)) or 0)
            gold = int(game.get("gold", 0) or 0)
            deck = len(game.get("deck") or [])
            deck_signature = self._deck_signature(game)
            inventory = tuple(str(x) for x in (game.get("relics") or [])) + tuple(
                str(x) for x in (game.get("potions") or []))
            max_hp = int(game.get("max_hp", 0) or 0)
            previous_ratio = (self.last_hp_snapshot / max_hp) if max_hp and self.last_hp_snapshot is not None else 1.0
            current_ratio = hp / max_hp if max_hp else 0.0
            if (gold != self.last_gold_snapshot or deck != self.last_deck_snapshot
                    or deck_signature != self.last_deck_signature
                    or inventory != self.last_inventory_snapshot
                    or self._combat_enemy_signature(game) != self.last_enemy_snapshot
                    or (self.last_hp_snapshot is not None and
                        previous_ratio > 0.30 >= current_ratio)):
                return True
        if screen != self.last_screen:
            return True
        if trigger in {"run_start", "act_change", "screen_change", "major_change", "player_deviation"}:
            return True
        return False

    def plan_for(self, game: dict, *, candidates=None, guide_rules=None,
                 trigger: str | None = None,
                 budget: DecisionBudget | None = None,
                 decision_id: str = "") -> tuple[StrategicPlan | None, BrainResponse]:
        if self._closed:
            return None, BrainResponse(
                backend=self.backend_name, error="strategic_planner_closed",
                fallback=True)
        candidates = list(candidates if candidates is not None else build_action_candidates(game))
        state_id = stable_state_id(game)
        self.memory.observe(game, state_id=state_id)
        if not self.planning_enabled:
            # Advice clones own only tactical state. Strategic work is started
            # and committed by the live owner loop, never once per clone/state.
            return self._usable_plan(game), self.last_response
        with self._plan_lock:
            has_pending_request = self._pending_request is not None
        if has_pending_request and not self._pending_request_covers(game, trigger):
            reason = (trigger if trigger in {
                "run_start", "act_change", "screen_change", "major_change",
                "player_deviation", "combat_start"}
                else "strategic_inputs_changed")
            self._invalidate_pending_request(reason)
        self._drain_plan_results(game, candidates, state_id)
        with self._plan_lock:
            has_pending_request = self._pending_request is not None
        if has_pending_request:
            return self._usable_plan(game), self.last_response
        if not self._needs_replan(game, trigger):
            return self._usable_plan(game), self.last_response
        actual_trigger = trigger or ("screen_change" if _screen(game) != self.last_screen else "state")
        previous_plan = self.current_plan
        self.plan_valid = False
        trigger_key = self._trigger_key(game)
        if self.async_planning:
            with self._plan_lock:
                same_request_pending = self._pending_request is not None
            if same_request_pending:
                return None, self.last_response
            self._record_plan_attempt(game, actual_trigger)
            self.generation += 1
            self._queue_plan(
                game, candidates, guide_rules, actual_trigger, previous_plan,
                state_id, trigger_key, budget, decision_id,
            )
            # The current game loop continues with JEV/rules.  A completed plan
            # is picked up by the next state poll, and stale results are dropped
            # in _drain_plan_results().
            return None, self.last_response
        self._record_plan_attempt(game, actual_trigger)
        self.generation += 1
        plan_started = time.monotonic()
        response = self.planner.plan(
            game, candidates, guide_rules=guide_rules, trigger=actual_trigger,
            memory=self.memory, previous=previous_plan, generation=self.generation,
            budget=budget, decision_id=decision_id,
            internal_timing={
                "worker_started_monotonic": time.monotonic(),
                "queue_wait_ms": 0,
            },
        )
        self.last_response = response
        self.last_state_id = state_id
        self.last_trigger_key = self._trigger_key(game)
        self.last_screen = _screen(game)
        self.last_hp_snapshot = int(game.get("current_hp", game.get("hp", 0)) or 0)
        self.last_gold_snapshot = int(game.get("gold", 0) or 0)
        self.last_deck_snapshot = len(game.get("deck") or [])
        self.last_deck_signature = self._deck_signature(game)
        self.last_inventory_snapshot = tuple(str(x) for x in (game.get("relics") or [])) + tuple(
            str(x) for x in (game.get("potions") or []))
        self.last_enemy_snapshot = self._combat_enemy_signature(game)
        self.plan_uses = 0
        if response.plan is not None:
            self.current_plan = response.plan
            self.plan_valid = True
            self._plan_screen = _screen(game)
            self._plan_act = int(game.get("act", 0) or 0)
            self._plan_floor = int(game.get("floor", game.get("floor_num", 0)) or 0)
            # Plan state is committed here, after the synchronous provider has
            # returned and the current state has already been observed.
            self.memory.set_plan(response.plan)
            self.context_plan = copy.deepcopy(response.plan)
            self.context_plan_reason = "current_plan"
            self.context_plan_state_id = state_id
            self.context_plan_act = int(game.get("act", 0) or 0)
            self.context_plan_run_epoch = self.memory.run_epoch
        elif previous_plan is not None and self._can_reuse_plan(previous_plan, game):
            self.current_plan = previous_plan
            self.plan_valid = True
            self.context_plan = copy.deepcopy(previous_plan)
            self.context_plan_reason = "cached_plan"
            self.context_plan_state_id = state_id
            self.context_plan_act = int(game.get("act", 0) or 0)
            self.context_plan_run_epoch = self.memory.run_epoch
        self._record_plan_result(
            run_id=self.memory.run_id, run_epoch=self.memory.run_epoch,
            decision_id=decision_id, state_id=state_id,
            request_id=response.request_id,
            plan_id=getattr(response.plan, "plan_id", ""),
            disposition=("committed_plan" if response.plan is not None else
                         "retained_previous" if self.plan_valid else "fallback"),
            commit_latency_ms=int((time.monotonic() - plan_started) * 1000),
            reason=response.error if response.fallback and not self.plan_valid else "",
        )
        return (self.current_plan if self.plan_valid else None), response

    def poll_plan_results(self, game: dict, *, candidates=None) -> PhaseStrategySnapshot:
        """Drain completed phase work without considering another request."""
        state_id = stable_state_id(game)
        candidates = list(candidates if candidates is not None
                          else build_action_candidates(game))
        self.memory.observe(game, state_id=state_id)
        self._drain_plan_results(game, candidates, state_id)
        return self.advice_snapshot(game)

    def choose(self, game: dict, fallback: dict, *, candidates=None,
               trigger: str | None = None, reason: str = "",
               jev_confidence: float = 0.0,
               guide_rules: list[dict] | None = None,
               plan: StrategicPlan | None = None,
               budget: DecisionBudget | None = None) -> tuple[dict, ExecutionDecision]:
        candidates = list(candidates if candidates is not None else build_action_candidates(game))
        # Planning is explicit in plan_for(). Arbitration cannot initiate I/O,
        # including when the earlier request failed or is still pending.
        response = self.last_response
        if plan is None:
            plan = self._usable_plan(game)
        # A late provider response may have lost its concrete candidate
        # bindings, but its scoped objective is still useful context.  Keep
        # that fact visible on the final decision when the current candidate
        # set is non-empty.  The context is metadata only: reconcile() still
        # receives no stale preferred/avoid IDs, so it cannot authorize an old
        # slot or bypass local legality checks.
        context_plan = self.context_plan
        context_only = bool(
            plan is None and context_plan is not None and candidates
            and context_plan.run_id == self.memory.run_id
            and self.context_plan_run_epoch == self.memory.run_epoch
            and self.context_plan_act > 0
            and int(game.get("act", 0) or 0) == self.context_plan_act
        )
        state_id = stable_state_id(game)
        command, decision = reconcile(
            game=game, candidates=candidates, fallback=fallback, plan=plan,
            state_id=state_id, jev_confidence=jev_confidence, reason=reason,
        )
        if context_only:
            decision.source_type = "gpt_context"
            decision.strategic_goal = context_plan.current_objective
            decision.plan_id = context_plan.plan_id
            decision.selection_basis = "strategic_context_local_choice"
            if not decision.reason:
                decision.reason = "按已接收的 GPT 战略目标，在当前合法候选中选择。"
        if response.error and decision.source_type == "rule_fallback":
            decision.reason = reason or "战略大脑不可用，沿用本地规则/JEV 建议。"
        self.plan_uses += 1
        return command, decision

    def current_detail(self, game: dict | None = None) -> dict:
        if game is not None:
            return self.detail_for(game)
        plan = self.current_plan if self.plan_valid else None
        return {
            "run_id": self.memory.run_id,
            "run_epoch": self.memory.run_epoch,
            "strategic_goal": plan.current_objective if plan else "",
            "long_term_goal": plan.long_term_goal if plan else "",
            "plan_id": plan.plan_id if plan else "",
            "brain_source": "gpt_strategy" if plan and self.plan_valid else "rule_fallback",
            "brain_backend": self.backend_name,
            "brain_error": self.last_response.error,
            "brain_latency_ms": self.last_response.latency_ms,
            "brain_request_id": self.last_response.request_id,
            "plan_scheduler": self._plan_scheduler_detail(),
            "strategic_context_retained": self.context_plan_retained,
            "strategic_context_reason": self.context_plan_reason,
        }

    def advice_snapshot(self, game: dict) -> PhaseStrategySnapshot:
        """Capture the current phase guidance for an isolated advice worker."""
        plan = self._usable_plan(game)
        context_plan = None
        context_reason = ""
        if plan is None and self.tactical_context_for(game) is not None:
            context_plan = copy.deepcopy(self.context_plan)
            context_plan.preferred_candidates = []
            context_plan.avoid_candidates = []
            context_plan.candidate_bindings = {}
            context_plan.avoid_bindings = {}
            context_plan.bindings_bound = False
            context_plan.state_id = stable_state_id(game)
            context_reason = self.context_plan_reason
        token = (f"plan:{plan.plan_id}" if plan is not None else
                 f"context:{context_plan.plan_id}" if context_plan is not None else "")
        return PhaseStrategySnapshot(
            run_id=self.memory.run_id,
            run_epoch=self.memory.run_epoch,
            state_id=stable_state_id(game),
            phase_key=self._strategic_stage_key(game),
            phase_token=token,
            screen_type=_screen(game),
            act=int(game.get("act", 0) or 0),
            floor=int(game.get("floor", game.get("floor_num", 0)) or 0),
            plan=plan,
            context_plan=context_plan,
            context_reason=context_reason,
            response=self.last_response,
        )

    def adopt_advice_snapshot(self, snapshot: PhaseStrategySnapshot) -> bool:
        """Install a detached strategy snapshot in a tactical-only worker."""
        if not isinstance(snapshot, PhaseStrategySnapshot):
            return False
        if (snapshot.run_id != self.memory.run_id
                or snapshot.run_epoch != self.memory.run_epoch):
            return False
        self.current_plan = copy.deepcopy(snapshot.plan)
        self.plan_valid = self.current_plan is not None
        self.context_plan = copy.deepcopy(snapshot.context_plan)
        self.context_plan_reason = snapshot.context_reason
        self.context_plan_state_id = snapshot.state_id
        self.context_plan_act = snapshot.act if self.context_plan is not None else 0
        self.context_plan_run_epoch = snapshot.run_epoch
        self._plan_screen = snapshot.screen_type if self.current_plan is not None else ""
        self._plan_act = snapshot.act if self.current_plan is not None else 0
        self._plan_floor = snapshot.floor if self.current_plan is not None else 0
        self.last_response = copy.deepcopy(snapshot.response or BrainResponse(
            backend=self.backend_name))
        plan_for_memory = self.current_plan or self.context_plan
        if plan_for_memory is not None:
            self.memory.set_plan(plan_for_memory)
        return True

    def _plan_scheduler_detail(self) -> dict:
        with self._plan_lock:
            pending = self._pending_request is not None
        return {
            "pending": pending,
            "planning_attempts": self.plan_attempts,
            "attempts_suppressed": self.plan_attempts_suppressed,
            "retry_cooldown_ms": self.plan_retry_cooldown_ms,
            "last_attempt_trigger": self._last_attempt_trigger,
            "invalidated_requests": self.invalidated_plan_requests,
            "invalidation_reasons": dict(self.plan_invalidation_reasons),
            "expired_results": self.expired_plan_results,
            "expired_result_reasons": dict(self.expired_plan_result_reasons),
            "replaced_results": self.dropped_plan_results,
        }

    def detail_for(self, game: dict) -> dict:
        """Return strategic metadata only when it is usable for this state."""
        plan = self._usable_plan(game)
        if plan is not None:
            return {
                "run_id": self.memory.run_id,
                "run_epoch": self.memory.run_epoch,
                "strategic_goal": plan.current_objective,
                "long_term_goal": plan.long_term_goal,
                "plan_id": plan.plan_id,
                "brain_source": "gpt_strategy",
                "brain_backend": self.backend_name,
                "brain_error": self.last_response.error,
                "brain_latency_ms": self.last_response.latency_ms,
                "brain_request_id": self.last_response.request_id,
                "plan_scheduler": self._plan_scheduler_detail(),
                "strategic_context_retained": self.context_plan_retained,
                "strategic_context_reason": self.context_plan_reason,
            }
        context_plan = self.context_plan
        if (context_plan is not None and self.context_plan_act > 0
                and int(game.get("act", 0) or 0) == self.context_plan_act
                and context_plan.run_id == self.memory.run_id
                and self.context_plan_run_epoch == self.memory.run_epoch):
            return {
                "run_id": self.memory.run_id,
                "run_epoch": self.memory.run_epoch,
                "strategic_goal": context_plan.current_objective,
                "long_term_goal": context_plan.long_term_goal,
                "plan_id": context_plan.plan_id,
                "brain_source": "gpt_context",
                "brain_backend": self.backend_name,
                "brain_error": self.last_response.error,
                "brain_latency_ms": self.last_response.latency_ms,
                "brain_request_id": self.last_response.request_id,
                "strategic_context_only": True,
                "strategic_context_reason": self.context_plan_reason,
                "strategic_context_retained": self.context_plan_retained,
                "plan_scheduler": self._plan_scheduler_detail(),
            }
        pending = bool(self._pending_key)
        return {
            "run_id": self.memory.run_id,
            "run_epoch": self.memory.run_epoch,
            "strategic_goal": "",
            "long_term_goal": "",
            "plan_id": "",
            "brain_source": "pending" if pending else "rule_fallback",
            "brain_backend": self.backend_name,
            "brain_error": self.last_response.error,
            "brain_latency_ms": self.last_response.latency_ms,
            "brain_request_id": self.last_response.request_id,
            "strategic_context_retained": self.context_plan_retained,
            "strategic_context_reason": self.context_plan_reason,
            "plan_scheduler": self._plan_scheduler_detail(),
        }
