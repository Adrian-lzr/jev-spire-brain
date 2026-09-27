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
)
from spirebrain.runtime_budget import DecisionBudget


def _screen(game: dict) -> str:
    return str((game or {}).get("screen_type", "")).upper()


class StrategicOrchestrator:
    """The only object the router needs to know about the GPT layer."""

    def __init__(self, *, backend: str = "openai", model: str | None = None,
                 endpoint: str | None = None,
                 timeout_ms: int = 6000, max_plan_steps: int = 5,
                 memory_events: int = 20, max_output_tokens: int = 900,
                 client=None, log_dir=None, async_planning: bool | None = None) -> None:
        provider = client if client is not None else get_strategic_brain(
            backend,
            model=model,
            endpoint=endpoint,
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
        self.memory = RunMemory(max_events=memory_events)
        self.planner = StrategicPlanner(self.provider, max_plan_steps=max_plan_steps,
                                        memory_events=memory_events)
        self.current_plan: StrategicPlan | None = None
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
        self.last_inventory_snapshot: tuple = ()
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

    @property
    def backend_name(self) -> str:
        return str(getattr(self.provider, "backend_name", "unknown"))

    def reset(self, run_id: str | None = None) -> None:
        # Keep the new run identity when the owning loop is resetting a plan;
        # dropping it would make terminal/result events look orphaned.
        self.memory.reset("" if run_id is None else str(run_id))
        self.current_plan = None
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
        self.last_inventory_snapshot = ()
        with self._plan_lock:
            # A running request cannot be cancelled portably.  Advancing
            # generation makes its eventual result harmless; queued work can be
            # dropped immediately so a new run starts cleanly.
            self._queued_plan = None
            self._pending_key = None
            self._pending_request = None
            self._pending_state_id = ""
            self._pending_run_id = ""
            self._plan_screen = ""
            self._plan_act = 0
            self._plan_floor = 0
            self._pending_scope = None

    def request_replan(self, reason: str = "player_deviation") -> None:
        """Invalidate the cached plan without discarding run memory."""
        self.plan_valid = False
        self.plan_uses = 0
        self.last_trigger_key = ""
        self.generation += 1
        with self._plan_lock:
            self._queued_plan = None
            self._pending_key = None
            self._pending_request = None
            self._pending_state_id = ""
            self._pending_run_id = ""
            self._pending_scope = None

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
                self._record_expired_plan_result(discard_reason)
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
            self.last_inventory_snapshot = tuple(str(x) for x in (live_game.get("relics") or [])) + tuple(
                str(x) for x in (live_game.get("potions") or []))
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
            elif previous is not None and self._can_reuse_plan(previous, live_game):
                # Keep a short cached plan through a transient timeout/invalid
                # JSON response.  Candidate reconciliation still filters any
                # item that disappeared from the live state.
                self.current_plan = previous
                self.plan_valid = True
            else:
                self.plan_valid = False

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

    def _usable_plan(self, game: dict) -> StrategicPlan | None:
        plan = self.current_plan if self.plan_valid else None
        return plan if self._can_reuse_plan(plan, game) else None

    def commit_worker_plan(self, plan: StrategicPlan | None, game: dict,
                           *, candidates=None) -> bool:
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
        current_candidates = list(candidates or ())
        if current_candidates:
            plan = self._rebind_plan(
                plan, game=game, candidates=current_candidates,
                state_id=stable_state_id(game),
            )
        else:
            plan = copy.deepcopy(plan)
        self.current_plan = plan
        self.plan_valid = True
        self._plan_screen = _screen(game)
        self._plan_act = int(game.get("act", 0) or 0)
        self._plan_floor = int(game.get("floor", game.get("floor_num", 0)) or 0)
        self.plan_uses = 0
        self.memory.set_plan(plan)
        return True

    def _queue_plan(self, game: dict, candidates, guide_rules, trigger: str,
                    previous: StrategicPlan | None, state_id: str,
                    trigger_key: str, budget: DecisionBudget | None = None,
                    decision_id: str = "") -> None:
        """Queue one latest-wins strategic request and return immediately."""
        run_id = self.memory.run_id
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
            self._queued_plan = job
            self.last_response = BrainResponse(
                backend=self.backend_name, error="战略规划进行中", fallback=True)
            if not self._plan_running:
                self._plan_running = True
                self._plan_thread = threading.Thread(
                    target=self._plan_worker, daemon=True,
                    name="SpireBrainStrategic")
                self._plan_thread.start()

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
                    response = self.planner.plan(
                        game, candidates, guide_rules=guide_rules, trigger=trigger,
                        memory=memory, previous=previous, generation=generation,
                        budget=budget, decision_id=decision_id,
                    )
                except Exception as exc:  # provider failures are non-fatal
                    response = BrainResponse(
                        backend=self.backend_name,
                        error=f"战略大脑异常：{type(exc).__name__}: {exc}",
                        fallback=True,
                    )
            result = DecisionResult(request=request, response=response)
            # A full slot always contains an older completion.  Replace it
            # atomically; the main loop still performs the authoritative
            # run/generation/scope check before committing this result.
            try:
                self._plan_results.put_nowait(result)
            except queue.Full:
                try:
                    self._plan_results.get_nowait()
                    self.dropped_plan_results += 1
                except queue.Empty:
                    pass
                try:
                    self._plan_results.put_nowait(result)
                except queue.Full:
                    # A concurrent consumer/producer won the race.  The
                    # worker is non-critical to the communication loop.
                    self.dropped_plan_results += 1

    def _trigger_key(self, game: dict) -> str:
        screen = _screen(game)
        act = game.get("act", 0)
        floor = game.get("floor", game.get("floor_num", 0))
        # Strategic changes, unlike combat animation fields, should expire a
        # plan.  Gold/deck/HP are enough to catch purchases and major damage.
        gold = game.get("gold", 0)
        hp = game.get("current_hp", game.get("hp", 0))
        deck = len(game.get("deck") or [])
        # Combat intentionally ignores hand/intent churn here; the tactical
        # layer refreshes every step without paying for a new strategic plan.
        if screen == "COMBAT":
            inventory = tuple(str(x) for x in (game.get("relics") or [])) + tuple(
                str(x) for x in (game.get("potions") or []))
            return f"{screen}:{act}:{floor}:{gold}:{hp}:{deck}:{inventory}"
        if screen in {"SHOP", "SHOP_SCREEN"}:
            state = game.get("screen_state") or game.get("screen") or {}
            try:
                shelf = json.dumps(state, sort_keys=True, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                shelf = repr(state)
            return f"{screen}:{act}:{floor}:{gold}:{hp}:{deck}:{shelf}"
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

        def deck(state: dict) -> tuple:
            cards = state.get("deck") or []
            return tuple(
                (str(card.get("id", card.get("name", ""))),
                 bool(card.get("upgraded", card.get("upgrades", 0))))
                if isinstance(card, dict) else str(card)
                for card in cards
            )

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
                or deck(previous) != deck(current)
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
            return False
        if _screen(game) == "COMBAT":
            return not self._combat_strategy_inputs_changed(request.state, game)
        return self._trigger_key(request.state) == self._trigger_key(game)

    def _invalidate_pending_request(self, reason: str) -> None:
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
            self._queued_plan = None

    def _record_expired_plan_result(self, reason: str) -> None:
        self.expired_plan_results += 1
        self.expired_plan_result_reasons[reason] = (
            self.expired_plan_result_reasons.get(reason, 0) + 1)

    def _needs_replan(self, game: dict, trigger: str | None = None) -> bool:
        if self.current_plan is None:
            return True
        screen = _screen(game)
        expiry = self.current_plan.expires_after or 5
        if screen == "COMBAT":
            # One combat card should never cause a second strategic request;
            # even a provider asking for expires_after=1 gets two tactical
            # steps before the next strategic boundary.
            expiry = max(2, expiry)
        if self.plan_uses >= expiry:
            return True
        key = self._trigger_key(game)
        if key != self.last_trigger_key and screen != "COMBAT":
            return True
        if screen == "COMBAT" and key != self.last_trigger_key:
            hp = int(game.get("current_hp", game.get("hp", 0)) or 0)
            gold = int(game.get("gold", 0) or 0)
            deck = len(game.get("deck") or [])
            inventory = tuple(str(x) for x in (game.get("relics") or [])) + tuple(
                str(x) for x in (game.get("potions") or []))
            max_hp = int(game.get("max_hp", 0) or 0)
            previous_ratio = (self.last_hp_snapshot / max_hp) if max_hp and self.last_hp_snapshot is not None else 1.0
            current_ratio = hp / max_hp if max_hp else 0.0
            if (gold != self.last_gold_snapshot or deck != self.last_deck_snapshot
                    or inventory != self.last_inventory_snapshot
                    or (self.last_hp_snapshot is not None and
                        (abs(hp - self.last_hp_snapshot) >= 2
                         or (previous_ratio > 0.30 >= current_ratio)))):
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
        with self._plan_lock:
            has_pending_request = self._pending_request is not None
        if has_pending_request and not self._pending_request_covers(game, trigger):
            reason = (trigger if trigger in {
                "run_start", "act_change", "screen_change", "major_change",
                "player_deviation", "combat_start"}
                else "strategic_inputs_changed")
            self._invalidate_pending_request(reason)
        self._drain_plan_results(game, candidates, state_id)
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
            self.generation += 1
            self._queue_plan(
                game, candidates, guide_rules, actual_trigger, previous_plan,
                state_id, trigger_key, budget, decision_id,
            )
            # The current game loop continues with JEV/rules.  A completed plan
            # is picked up by the next state poll, and stale results are dropped
            # in _drain_plan_results().
            return None, self.last_response
        self.generation += 1
        response = self.planner.plan(
            game, candidates, guide_rules=guide_rules, trigger=actual_trigger,
            memory=self.memory, previous=previous_plan, generation=self.generation,
            budget=budget, decision_id=decision_id,
        )
        self.last_response = response
        self.last_state_id = state_id
        self.last_trigger_key = self._trigger_key(game)
        self.last_screen = _screen(game)
        self.last_hp_snapshot = int(game.get("current_hp", game.get("hp", 0)) or 0)
        self.last_gold_snapshot = int(game.get("gold", 0) or 0)
        self.last_deck_snapshot = len(game.get("deck") or [])
        self.last_inventory_snapshot = tuple(str(x) for x in (game.get("relics") or [])) + tuple(
            str(x) for x in (game.get("potions") or []))
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
        elif previous_plan is not None and self._can_reuse_plan(previous_plan, game):
            self.current_plan = previous_plan
            self.plan_valid = True
        return (self.current_plan if self.plan_valid else None), response

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
        state_id = stable_state_id(game)
        command, decision = reconcile(
            game=game, candidates=candidates, fallback=fallback, plan=plan,
            state_id=state_id, jev_confidence=jev_confidence, reason=reason,
        )
        if response.error and decision.source_type == "rule_fallback":
            decision.reason = reason or "战略大脑不可用，沿用本地规则/JEV 建议。"
        self.plan_uses += 1
        return command, decision

    def current_detail(self) -> dict:
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
        }

    def _plan_scheduler_detail(self) -> dict:
        with self._plan_lock:
            pending = self._pending_request is not None
        return {
            "pending": pending,
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
            "plan_scheduler": self._plan_scheduler_detail(),
        }
