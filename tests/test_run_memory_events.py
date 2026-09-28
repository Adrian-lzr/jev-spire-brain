"""Regression tests for the main-loop memory/event boundary."""

from __future__ import annotations

import pytest

from spirebrain.brain.memory import (
    ADVICE_HISTORY,
    DEVIATION_RECORD,
    EXECUTION_ATTEMPT,
    FACT_MEMORY,
    RESULT_RECORD,
    RunMemory,
    UNOBSERVED_RECORD,
)
from spirebrain.brain.orchestrator import StrategicOrchestrator
from spirebrain.brain.protocol import ActionCandidate, BrainResponse, StrategicPlan
from spirebrain.brain.action_broker import build_action_candidates


def _state(run_id="run-a", screen="COMBAT"):
    return {
        "run_id": run_id,
        "seed": "same-seed",
        "character": "IRONCLAD",
        "act": 1,
        "floor": 2,
        "current_hp": 50,
        "max_hp": 80,
        "gold": 99,
        "screen_type": screen,
        "deck": [{"id": "Strike_R"}],
    }


def test_explanation_is_not_recorded_as_execution_result():
    memory = RunMemory()
    memory.observe(_state(), state_id="s1")
    memory.record_action({"command": "end"}, state_id="s1", result="保留能量")

    event = memory.events[-1]
    assert event["kind"] == EXECUTION_ATTEMPT
    assert event["reason"] == "保留能量"
    assert "result" not in event


def test_background_snapshot_is_deeply_immutable_and_does_not_follow_live_memory():
    memory = RunMemory()
    memory.observe(_state(), state_id="s1")
    snapshot = memory.snapshot()
    memory.record_deviation("end", "play", state_id="s1")

    assert snapshot.digest()["deviations"] == []
    with pytest.raises(TypeError):
        snapshot.events[0]["state_id"] = "changed"


def test_replacing_unstarted_strategic_request_records_skipped_decision():
    from spirebrain.brain.planner import stable_state_id

    owner = StrategicOrchestrator(backend="mock", async_planning=True)
    owner.memory.reset("run-a")
    owner._plan_running = True  # Keep jobs queued and make replacement deterministic.
    events = []
    owner.event_sink = lambda kind, fields: events.append((kind, fields))

    first = _state(screen="EVENT")
    first["screen_state"] = {"options": ["left", "right"]}
    owner.generation = 1
    owner._queue_plan(first, [], [], "event_choice", None,
                      stable_state_id(first), "first", decision_id="decision-a")
    second = _state(screen="EVENT")
    second["screen_state"] = {"options": ["left", "right", "leave"]}
    owner.generation = 2
    owner._queue_plan(second, [], [], "event_choice", None,
                      stable_state_id(second), "second", decision_id="decision-b")

    skipped = [fields for kind, fields in events if kind == "decision_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["decision_id"] == "decision-a"
    assert skipped[0]["state_id"] == stable_state_id(first)
    assert skipped[0]["reason"] == "superseded_before_start"
    owner._queued_plan = None


def test_strategic_completion_queue_replacement_records_expired_result():
    from spirebrain.brain.protocol import (
        BrainResponse, DecisionContext, DecisionRequest, DecisionResult,
        RunSessionSnapshot,
    )

    owner = StrategicOrchestrator(backend="mock", async_planning=True)
    events = []
    owner.event_sink = lambda kind, fields: events.append((kind, fields))

    def result(decision_id, state_id, request_id):
        context = DecisionContext(
            run=RunSessionSnapshot(run_id="run-a", run_epoch=1),
            state_id=state_id, decision_id=decision_id, generation=1,
            state={"screen_type": "EVENT"},
        )
        request = DecisionRequest(context=context, state=context.state,
                                  memory=owner.memory.snapshot())
        return DecisionResult(request=request,
                              response=BrainResponse(request_id=request_id,
                                                     backend="fixture"))

    owner._plan_results.put_nowait(result("decision-old", "state-old", "request-old"))
    owner._enqueue_plan_result(result("decision-new", "state-new", "request-new"))

    expired = [fields for kind, fields in events if kind == "expired_result"]
    assert len(expired) == 1
    assert expired[0]["decision_id"] == "decision-old"
    assert expired[0]["state_id"] == "state-old"
    assert expired[0]["request_id"] == "request-old"
    assert expired[0]["fallback_reason"] == "completion_queue_replaced"
    assert owner.expired_plan_result_reasons["completion_queue_replaced"] == 1


def test_event_kinds_keep_advice_action_fact_result_and_unknown_separate():
    memory = RunMemory()
    memory.observe(_state(), state_id="s1")
    memory.record_advice({
        "state_id": "s1", "decision_id": "d1", "label": "结束回合",
        "reason": "保留能量", "command": {"command": "end"},
    })
    memory.record_execution_attempt({"command": "wait"}, state_id="s1",
                                    decision_id="d1", status="sent")
    memory.record_observed_action({"label": "出 Strike_R"}, state_id="s2",
                                  decision_id="d1", observed_state=_state())
    memory.record_observed_result("victory", state_id="s3", decision_id="d1")
    memory.record_deviation("结束回合", "出 Strike_R", state_id="s2",
                            decision_id="d1", evidence={"hand_lost": {"Strike_R": 1}})
    memory.record_unobserved(state_id="s4", decision_id="d2", reason="界面跳转")

    kinds = [event["kind"] for event in memory.events]
    assert ADVICE_HISTORY in kinds
    assert EXECUTION_ATTEMPT in kinds
    assert FACT_MEMORY in kinds
    assert RESULT_RECORD in kinds
    assert DEVIATION_RECORD in kinds
    assert UNOBSERVED_RECORD in kinds
    assert memory.events[-1]["kind"] == UNOBSERVED_RECORD


def test_unobserved_feedback_keeps_the_state_evidence_without_guessing_action():
    memory = RunMemory()
    observed = _state()
    event = memory.record_unobserved(state_id="s2", decision_id="d2",
                                     evidence={"why": "screen changed"},
                                     observed_state=observed)
    assert event["kind"] == UNOBSERVED_RECORD
    assert event["observed_state"] == observed
    assert "actual" not in event


def test_agent_resets_terminal_result_before_same_seed_reentry(tmp_path):
    from spirebrain.driver.agent import SpireBrainAgent

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    agent.observe(_state())
    agent.observe(_state("run-a", "DEATH"))
    terminal_epoch = agent.run_session.run_epoch
    assert any(event["kind"] == RESULT_RECORD
               for event in agent.strategic.memory.events)

    agent.observe(_state())
    assert agent.run_session.run_epoch > terminal_epoch
    assert all(event["kind"] != RESULT_RECORD
               for event in agent.strategic.memory.events)
    assert all(event.get("state_id") != "old" for event in agent.strategic.memory.events)


def test_execution_attempt_is_committed_only_after_play_pipe_write():
    import io
    from spirebrain.driver.stdio import StdioTransport

    class Recorder:
        def __init__(self):
            self.attempts = []

        def choose_action(self, game):
            return {"command": "end"}

        def record_execution_attempt(self, command, **kwargs):
            self.attempts.append((command, kwargs))

    recorder = Recorder()
    transport = StdioTransport(recorder, mode="play", log_path=None, advice_path=None)
    message = {
        "in_game": True, "ready_for_command": True,
        "available_commands": ["end", "wait", "state"],
        "game_state": {"screen_type": "COMBAT", "act": 1, "floor": 1},
    }
    out = io.StringIO()
    transport.run([__import__("json").dumps(message)], out, send_ready=False)
    assert out.getvalue().startswith("end\n")
    assert len(recorder.attempts) == 1
    assert recorder.attempts[0][0]["_wire"] == "end"


def test_latest_state_wins_when_a_disappears_and_reappears():
    import threading
    import time

    started = threading.Event()
    release = threading.Event()

    class Provider:
        backend_name = "openai"
        async_required = True

        def __init__(self):
            self.calls = []

        def plan(self, payload):
            self.calls.append(payload["state_id"])
            started.set()
            release.wait(1)
            n = len(self.calls)
            plan = StrategicPlan.from_dict({
                "plan_id": f"p{n}", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": f"state-{n}",
                "long_term_goal": "win", "priority": [],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "test", "uncertainty": "",
                "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend="openai")

    provider = Provider()
    orchestrator = StrategicOrchestrator(client=provider, backend="openai")
    a = _state("run-a")
    a["available_commands"] = ["wait", "state"]
    b = dict(a, floor=3, current_hp=49)
    orchestrator.plan_for(a, candidates=build_action_candidates(a))
    assert started.wait(1)
    orchestrator.plan_for(b, candidates=build_action_candidates(b))
    orchestrator.plan_for(a, candidates=build_action_candidates(a))
    release.set()

    accepted = None
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        accepted, _ = orchestrator.plan_for(a, candidates=build_action_candidates(a))
        if accepted is not None:
            break
        time.sleep(0.005)
    assert accepted is not None
    # The first A response is stale; only the latest A generation can publish.
    assert accepted.plan_id == "p2"
    assert orchestrator.memory.plan_id == "p2"


def test_inflight_combat_request_is_not_replaced_by_poll_noise():
    import threading
    import time

    started = threading.Event()
    release = threading.Event()

    class Provider:
        backend_name = "openai"
        async_required = True
        calls = 0

        def plan(self, payload):
            type(self).calls += 1
            started.set()
            release.wait(1)
            plan = StrategicPlan.from_dict({
                "plan_id": "combat-plan", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "先保证生存",
                "long_term_goal": "通关", "priority": ["survive"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "combat", "uncertainty": "",
                "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend="openai")

    provider = Provider()
    orchestrator = StrategicOrchestrator(client=provider, backend="openai")
    first = _state("run-a")
    first["available_commands"] = ["wait", "state"]
    # A normal hit while still above the danger threshold is tactical churn;
    # it must not cancel a slow strategic request. Candidate reconciliation at
    # commit time still binds the plan to the current state.
    second = dict(first, current_hp=45, animation_frame=2, poll_uuid="noise")
    orchestrator.plan_for(first, candidates=build_action_candidates(first))
    assert started.wait(1)
    orchestrator.plan_for(second, candidates=build_action_candidates(second))
    assert provider.calls == 1
    release.set()
    deadline = time.monotonic() + 1
    accepted = None
    while time.monotonic() < deadline:
        accepted, _ = orchestrator.plan_for(second, candidates=build_action_candidates(second))
        if accepted is not None:
            break
        time.sleep(0.005)
    assert accepted is not None
    assert accepted.current_objective == "先保证生存"


def test_changed_event_options_supersede_same_scope_inflight_plan(tmp_path):
    """A late answer for old event choices must not replace the current plan."""
    import threading

    from spirebrain.brain.protocol import BrainResponse, StrategicPlan
    from spirebrain.brain.orchestrator import StrategicOrchestrator
    from spirebrain.brain.planner import stable_state_id

    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    second_returning = threading.Event()

    class Provider:
        backend_name = "openai"
        async_required = True

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            call = self.calls
            if call == 1:
                first_started.set()
                assert release_first.wait(2)
                objective = "old event"
            else:
                second_started.set()
                assert release_second.wait(2)
                objective = "current event"
                second_returning.set()
            plan = StrategicPlan.from_dict({
                "plan_id": f"event-{call}", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": objective,
                "long_term_goal": "finish the run", "priority": [],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "controlled fixture",
                "uncertainty": "", "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend="openai")

    provider = Provider()
    owner = StrategicOrchestrator(client=provider, backend="openai", log_dir=tmp_path)
    trace_events = []
    owner.event_sink = lambda kind, fields: trace_events.append((kind, fields))
    owner.provider.event_sink = owner.event_sink
    old_state = {
        "run_id": "run-event", "screen_type": "EVENT", "act": 1, "floor": 8,
        "screen_state": {"options": ["trade", "leave"]},
        "available_commands": ["choose", "wait", "state"],
    }
    current_state = dict(old_state, screen_state={"options": ["fight", "leave"]})

    owner.plan_for(old_state, candidates=[], decision_id="decision-old")
    assert first_started.wait(1)
    poll_noise = dict(old_state, poll_uuid="irrelevant-poll-change")
    owner.plan_for(poll_noise, candidates=[])
    assert owner._queued_plan is None
    assert owner.invalidated_plan_requests == 0
    owner.plan_for(current_state, candidates=[], decision_id="decision-new")

    # The event options changed within the same screen/act/floor scope. A new
    # task must replace the pending one even though the first HTTP call cannot
    # be cancelled.
    assert owner._queued_plan is not None
    assert owner._pending_state_id == stable_state_id(current_state)

    release_first.set()
    assert second_started.wait(1)
    accepted, _ = owner.plan_for(current_state, candidates=[])
    assert accepted is None
    assert owner.memory.plan_id != "event-1"
    context_detail = owner.detail_for(current_state)
    assert context_detail["brain_source"] == "gpt_context"
    assert context_detail["strategic_context_only"] is True
    assert owner.expired_plan_result_reasons["superseded_request"] == 1
    first_provider_request = next(fields for kind, fields in trace_events
                                  if kind == "provider_request"
                                  and fields.get("decision_id") == "decision-old")
    first_expiry = next(fields for kind, fields in trace_events
                        if kind == "expired_result"
                        and fields.get("decision_id") == "decision-old")
    assert first_expiry["request_id"] == first_provider_request["request_id"]
    assert first_expiry["state_id"] == first_provider_request["state_id"]

    release_second.set()
    assert second_returning.wait(1)
    result = owner._plan_results.get(timeout=1)
    owner._plan_results.put_nowait(result)
    accepted, _ = owner.plan_for(current_state, candidates=[])
    assert accepted is not None
    assert accepted.plan_id == "event-2"
    assert owner.memory.plan_id == "event-2"
    assert owner.invalidated_plan_requests == 1
    assert owner.plan_invalidation_reasons == {"strategic_inputs_changed": 1}


def test_late_combat_plan_keeps_strategy_but_rebinds_changed_candidates():
    """A successful slow strategic response must not disappear on hand churn."""
    import threading
    import time

    started = threading.Event()
    release = threading.Event()

    class Provider:
        backend_name = "deepseek"
        async_required = True

        def plan(self, payload):
            started.set()
            release.wait(1)
            preferred = payload["candidates"][0]["candidate_id"]
            return BrainResponse(plan=StrategicPlan.from_dict({
                "plan_id": "slow-plan", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "保留战略目标",
                "long_term_goal": "通关", "priority": ["survive"],
                "preferred_candidates": [preferred], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "慢响应", "uncertainty": "",
                "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"]), backend="deepseek")

    def candidate(label):
        return ActionCandidate(candidate_id="combat:play:0:0", kind="combat",
                               label=label, command={"command": "play", "card": 0,
                               "target": 0})

    provider = Provider()
    orchestrator = StrategicOrchestrator(client=provider, backend="deepseek")
    first = _state()
    first["hand"] = [{"id": "Strike_R", "name": "Strike", "cost": 1}]
    second = dict(first, current_hp=47, hand=[{"id": "Defend_R", "name": "Defend", "cost": 1}])
    orchestrator.plan_for(first, candidates=[candidate("Strike")])
    assert started.wait(1)
    orchestrator.plan_for(second, candidates=[candidate("Defend")])
    release.set()

    accepted = None
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        accepted, _ = orchestrator.plan_for(second, candidates=[candidate("Defend")])
        if accepted is not None:
            break
        time.sleep(0.005)
    assert accepted is not None
    assert accepted.current_objective == "保留战略目标"
    # The old positional candidate has a different signature and is removed;
    # it cannot silently select Defend just because the ID stayed the same.
    assert accepted.preferred_candidates == []


def test_legacy_plan_without_candidate_signatures_keeps_goal_not_slot_ids():
    state = _state()
    candidates = [ActionCandidate(
        candidate_id="combat:play:0:0", kind="combat", label="新牌",
        command={"command": "play", "card": 0, "target": 0},
    )]
    plan = StrategicPlan(
        plan_id="legacy", state_id="old", run_id="run-a",
        current_objective="维持战略目标",
        preferred_candidates=["combat:play:0:0"],
        avoid_candidates=["combat:play:0:0"],
    )
    rebound = StrategicOrchestrator._rebind_plan(
        plan, game=state, candidates=candidates, state_id="new",
    )
    assert rebound.current_objective == "维持战略目标"
    assert rebound.preferred_candidates == []
    assert rebound.avoid_candidates == []


def test_late_strategic_result_is_display_context_but_not_an_action_plan():
    owner = StrategicOrchestrator(backend="deepseek", async_planning=True)
    owner.memory.reset("run-a")
    stale = StrategicPlan(
        plan_id="late-plan", state_id="old-state", run_id="run-a",
        current_objective="保留生命并为精英战准备输出",
        long_term_goal="稳健通关", resource_constraints={"gold_floor": 80},
    )
    current = _state(screen="CARD_REWARD")
    owner._retain_context_plan(stale, current, "superseded_request")

    detail = owner.detail_for(current)
    assert detail["brain_source"] == "gpt_context"
    assert detail["strategic_context_only"] is True
    assert detail["strategic_goal"] == "保留生命并为精英战准备输出"

    # Context is never passed to arbitration as a concrete candidate plan.
    command, decision = owner.choose(
        current, {"command": "state"}, candidates=[],
    )
    assert command == {"command": "state"}
    assert decision.source_type == "rule_fallback"

    owner.reset(run_id="run-b")
    assert owner.context_plan is None
    assert owner.detail_for(_state(run_id="run-b"))["brain_source"] != "gpt_context"


def test_tactical_context_is_scoped_to_run_epoch_and_act():
    owner = StrategicOrchestrator(backend="deepseek", async_planning=True)
    owner.memory.reset("run-a")
    plan = StrategicPlan(
        plan_id="context-plan", state_id="old-state", run_id="run-a",
        current_objective="为精英战保留资源",
        preferred_candidates=["combat:play:0:0"],
        avoid_candidates=["combat:end"],
    )
    current = _state(run_id="run-a", screen="COMBAT")
    owner._retain_context_plan(plan, current, "superseded_request")

    context = owner.tactical_context_for(current)
    assert context == {
        "plan_id": "context-plan",
        "goal": "为精英战保留资源",
        "resource_constraints": {},
        "context_only": True,
    }
    assert owner.tactical_context_for(dict(current, act=2)) is None
    assert owner.tactical_context_for(dict(current, run_id="run-b")) is None

    # Reusing the same run id after a lifecycle reset must not resurrect context.
    owner.memory.reset("run-a")
    assert owner.tactical_context_for(current) is None


def test_reward_transition_requires_a_new_scene_plan():
    import threading
    import time

    started = threading.Event()
    release = threading.Event()

    class Provider:
        backend_name = "deepseek"
        async_required = True
        calls = []

        def plan(self, payload):
            self.calls.append(payload["state_id"])
            started.set()
            release.wait(1)
            return BrainResponse(plan=StrategicPlan.from_dict({
                "plan_id": "cross-scene", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "优先保留高价值奖励",
                "long_term_goal": "通关", "priority": ["deck"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "战略上下文", "uncertainty": "",
                "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"]), backend="deepseek")

    provider = Provider()
    orchestrator = StrategicOrchestrator(client=provider, backend="deepseek")
    combat = _state()
    reward = dict(combat, screen_type="CARD_REWARD", floor=3,
                  screen_state={"cards": [{"id": "PommelStrike"}]})
    orchestrator.plan_for(combat, candidates=build_action_candidates(combat))
    assert started.wait(1)
    orchestrator.plan_for(reward, candidates=build_action_candidates(reward))
    release.set()
    deadline = time.monotonic() + 1
    accepted = None
    while time.monotonic() < deadline:
        accepted, _ = orchestrator.plan_for(reward, candidates=build_action_candidates(reward))
        if accepted is not None:
            break
        time.sleep(0.005)
    assert accepted is not None
    assert accepted.current_objective == "优先保留高价值奖励"
    assert accepted.state_id == orchestrator.last_state_id
    assert len(provider.calls) == 2
    assert provider.calls[-1] == accepted.state_id


def test_old_result_cannot_commit_after_same_run_id_is_reset():
    import threading
    import time

    started = threading.Event()
    release = threading.Event()

    class Provider:
        backend_name = "openai"
        async_required = True

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            started.set()
            release.wait(1)
            plan = StrategicPlan.from_dict({
                "plan_id": f"run-{self.calls}", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "new run",
                "long_term_goal": "win", "priority": [],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "test", "uncertainty": "",
                "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend="openai")

    provider = Provider()
    orchestrator = StrategicOrchestrator(client=provider, backend="openai")
    state = _state("same-run-id")
    orchestrator.plan_for(state, candidates=build_action_candidates(state))
    assert started.wait(1)
    old_epoch = orchestrator.memory.run_epoch
    orchestrator.reset(run_id="same-run-id")
    assert orchestrator.memory.run_epoch > old_epoch
    orchestrator.plan_for(state, candidates=build_action_candidates(state))
    release.set()

    accepted = None
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        accepted, _ = orchestrator.plan_for(state, candidates=build_action_candidates(state))
        if accepted is not None:
            break
        time.sleep(0.005)
    assert accepted is not None
    assert accepted.plan_id == "run-2"
    assert orchestrator.memory.plan_id == "run-2"


def test_same_seed_new_run_isolated_by_epoch_even_when_run_id_repeats():
    memory = RunMemory()
    memory.observe(_state("run-a"), state_id="old")
    first_epoch = memory.run_epoch
    memory.record_observed_action({"label": "旧局行动"}, state_id="old")

    # A menu/death edge resets facts. Re-entering with the same seed/run id is
    # still a new run and must not inherit the old fact or plan.
    memory.observe(_state("run-a", "MENU"), state_id="menu")
    memory.observe(_state("run-a"), state_id="new")
    assert memory.run_epoch > first_epoch
    assert all(event.get("state_id") != "old" for event in memory.events)
    assert not memory.deviations
