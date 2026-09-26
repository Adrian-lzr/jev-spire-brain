"""Coordinates the strategic provider, bounded run memory and action broker."""

from __future__ import annotations

import json
import copy
import queue
import threading

from .action_broker import build_action_candidates, reconcile
from .gpt_client import LoggingStrategicClient, get_strategic_brain
from .memory import RunMemory
from .planner import StrategicPlanner, stable_state_id
from .protocol import BrainResponse, ExecutionDecision, StrategicPlan
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
        self._queued_plan = None
        self._plan_results: queue.Queue = queue.Queue()
        self._pending_key: tuple[str, int, str, int] | None = None
        self._pending_state_id = ""
        self._pending_run_id = ""
        self._plan_screen = ""
        self._plan_act = 0
        self._plan_floor = 0
        # A strategic request is scoped to a scene/act/floor. Combat hand and
        # HP churn belongs to the tactical layer and must not supersede an
        # in-flight provider request on every poll.
        self._pending_scope: tuple[str, int, int] | None = None

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
            self._pending_state_id = ""
            self._pending_run_id = ""
            self._pending_scope = None

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
            (result_state_id, result_run_id, result_run_epoch, generation, trigger_key,
             result_game, previous,
             response) = result
            current_game = game or {}
            current_state_id = state_id or stable_state_id(current_game)
            current_scope = self._strategic_scope(current_game) if game is not None else None
            with self._plan_lock:
                exact = self._pending_key == (result_state_id, generation,
                                              result_run_id, result_run_epoch)
                same_scope = (current_scope is None or
                              self._pending_scope == current_scope)
            # Combat polling changes the state ID frequently, but the
            # scene/act/floor scope still makes the strategic context valid.
            # A scene transition does not: it must trigger a fresh request.
            current = exact or same_scope
            if (not current or generation != self.generation
                    or result_run_id != self.memory.run_id
                    or result_run_epoch != self.memory.run_epoch):
                # The player already changed screens/runs.  A late strategic
                # answer must never overwrite the current recommendation.
                continue
            with self._plan_lock:
                self._pending_key = None
                self._pending_state_id = ""
                self._pending_run_id = ""
                self._pending_scope = None
            self.last_response = response
            live_game = current_game if game is not None else result_game
            live_state_id = current_state_id if game is not None else result_state_id
            self.last_state_id = live_state_id
            self.last_trigger_key = self._trigger_key(live_game) if game is not None else trigger_key
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

    def _queue_plan(self, game: dict, candidates, guide_rules, trigger: str,
                    previous: StrategicPlan | None, state_id: str,
                    trigger_key: str, budget: DecisionBudget | None = None) -> None:
        """Queue one latest-wins strategic request and return immediately."""
        run_id = self.memory.run_id
        with self._plan_lock:
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
            job = (game_copy, candidate_copy,
                   list(guide_rules or []), trigger, previous, self.generation,
                   state_id, run_id, run_epoch, trigger_key, memory)
            job = job + (budget,)
            self._pending_key = key
            self._pending_state_id = state_id
            self._pending_run_id = run_id
            self._pending_scope = self._strategic_scope(game)
            self._queued_plan = job
            self.last_response = BrainResponse(
                backend=self.backend_name, error="战略规划进行中", fallback=True)
            if not self._plan_running:
                self._plan_running = True
                threading.Thread(target=self._plan_worker, daemon=True,
                                 name="SpireBrainStrategic").start()

    def _plan_worker(self) -> None:
        while True:
            with self._plan_lock:
                job = self._queued_plan
                self._queued_plan = None
                if job is None:
                    self._plan_running = False
                    return
            (game, candidates, guide_rules, trigger, previous, generation,
             state_id, run_id, run_epoch, trigger_key, memory, budget) = job
            try:
                response = self.planner.plan(
                    game, candidates, guide_rules=guide_rules, trigger=trigger,
                    memory=memory, previous=previous, generation=generation,
                    budget=budget,
                )
            except Exception as exc:  # provider failures are non-fatal
                response = BrainResponse(
                    backend=self.backend_name,
                    error=f"战略大脑异常：{type(exc).__name__}: {exc}",
                    fallback=True,
                )
            self._plan_results.put((state_id, run_id, run_epoch, generation, trigger_key,
                                   game, previous, response))

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
                 budget: DecisionBudget | None = None) -> tuple[StrategicPlan | None, BrainResponse]:
        candidates = list(candidates if candidates is not None else build_action_candidates(game))
        state_id = stable_state_id(game)
        self.memory.observe(game, state_id=state_id)
        self._drain_plan_results(game, candidates, state_id)
        if not self._needs_replan(game, trigger):
            return self._usable_plan(game), self.last_response
        actual_trigger = trigger or ("screen_change" if _screen(game) != self.last_screen else "state")
        previous_plan = self.current_plan
        self.plan_valid = False
        trigger_key = self._trigger_key(game)
        if self.async_planning:
            with self._plan_lock:
                # Keep one request for the current strategic scope. In combat,
                # each hand/HP animation is a tactical update; replacing the
                # request makes a valid result almost impossible to publish.
                same_request_pending = (
                    self._pending_run_id == self.memory.run_id
                    and self._pending_scope == self._strategic_scope(game)
                )
            if same_request_pending:
                return None, self.last_response
            self.generation += 1
            self._queue_plan(
                game, candidates, guide_rules, actual_trigger, previous_plan,
                state_id, trigger_key, budget,
            )
            # The current game loop continues with JEV/rules.  A completed plan
            # is picked up by the next state poll, and stale results are dropped
            # in _drain_plan_results().
            return None, self.last_response
        self.generation += 1
        response = self.planner.plan(
            game, candidates, guide_rules=guide_rules, trigger=actual_trigger,
            memory=self.memory, previous=previous_plan, generation=self.generation,
            budget=budget,
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
        }
