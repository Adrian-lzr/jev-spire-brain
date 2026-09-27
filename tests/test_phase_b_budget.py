from spirebrain.runtime_budget import DecisionBudget


def test_zero_model_call_budget_is_a_hard_disable():
    budget = DecisionBudget(total_ms=1000, max_calls=0)
    assert budget.reserve(100) == 0
    assert budget.calls == 0
    assert budget.exhausted == "max_model_calls"


def test_orchestrator_choose_reuses_supplied_plan_without_planning():
    from spirebrain.brain.orchestrator import StrategicOrchestrator
    from spirebrain.brain.protocol import StrategicPlan

    class Provider:
        backend_name = "spy"
        def __init__(self):
            self.calls = 0
        def plan(self, payload):
            self.calls += 1
            raise AssertionError("choose must not implicitly replan")

    provider = Provider()
    orchestrator = StrategicOrchestrator(client=provider, async_planning=False)
    game = {"screen_type": "REST", "available_commands": ["choose"],
            "screen_state": {"rest_options": ["rest"]}}
    plan = StrategicPlan.from_dict({
        "plan_id": "p", "state_id": "",
        "run_id": "local", "current_objective": "survive",
        "long_term_goal": "finish", "priority": [],
        "preferred_candidates": [], "avoid_candidates": [],
        "resource_constraints": {}, "next_steps": [],
        "replan_triggers": [], "reason": "fixture",
        "uncertainty": "", "expires_after": 1,
    }, state_id="", run_id="local")
    # Empty candidate set keeps the test focused on avoiding an implicit call.
    command, _ = orchestrator.choose(game, {"command": "state"},
                                     candidates=[], plan=plan)
    assert command["command"] == "state"
    assert provider.calls == 0


def test_failed_plan_does_not_trigger_second_request_during_arbitration():
    from spirebrain.brain.orchestrator import StrategicOrchestrator
    from spirebrain.brain.protocol import BrainResponse

    class Provider:
        backend_name = "scripted"
        calls = 0

        def plan(self, payload):
            self.calls += 1
            return BrainResponse(error="scripted failure", fallback=True)

    provider = Provider()
    owner = StrategicOrchestrator(client=provider, async_planning=False)
    game = {"run_id": "r", "screen_type": "REST"}
    owner.plan_for(game, candidates=[])
    owner.choose(game, {"command": "state"}, candidates=[])
    assert provider.calls == 1


def test_runtime_zero_call_setting_is_preserved(tmp_path):
    from spirebrain.runtime_config import resolve_runtime_config

    config = resolve_runtime_config(tmp_path, environ={"MAX_MODEL_CALLS": "0"})
    assert config.max_model_calls == 0


def test_queued_plan_expired_before_worker_start_does_not_call_provider():
    from spirebrain.brain.orchestrator import StrategicOrchestrator
    from spirebrain.brain.protocol import BrainResponse

    now = [10.0]

    class Provider:
        backend_name = "spy"

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            return BrainResponse(error="should not be called", fallback=True)

    provider = Provider()
    orchestrator = StrategicOrchestrator(client=provider, async_planning=True)
    orchestrator._plan_running = True  # deterministic stand-in for a busy worker
    budget = DecisionBudget(total_ms=10, max_calls=1, clock=lambda: now[0],
                            started_at=10.0)
    game = {"run_id": "run-a", "screen_type": "EVENT",
            "screen_state": {"options": ["left", "right"]}}
    orchestrator.memory.observe(game)

    orchestrator._queue_plan(
        game, [], [], "event", None, "state-a", "trigger-a",
        budget=budget, decision_id="decision-a")
    now[0] = 10.1  # the request expires while waiting behind the active task
    orchestrator._plan_worker()

    assert provider.calls == 0
    assert orchestrator._plan_results.qsize() == 1
    result = orchestrator._plan_results.get_nowait()
    assert result.response.error == "decision_deadline_before_start"


def test_orchestrator_close_wait_is_bounded_and_invalidates_late_result():
    import threading

    from spirebrain.brain.orchestrator import StrategicOrchestrator
    from spirebrain.brain.protocol import BrainResponse

    started = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    class Provider:
        backend_name = "controlled"
        async_required = True

        def plan(self, payload):
            started.set()
            assert release.wait(2)
            returned.set()
            return BrainResponse(error="late", fallback=True)

    owner = StrategicOrchestrator(client=Provider(), backend="openai")
    state = {"run_id": "close-run", "screen_type": "EVENT", "act": 1,
             "floor": 1, "screen_state": {"options": ["a", "b"]}}
    owner.plan_for(state, candidates=[])
    assert started.wait(1)

    # The worker is inside an uninterruptible provider call. Shutdown returns
    # at its configured wait bound while invalidating the eventual response.
    assert owner.close(timeout_ms=5) is False
    release.set()
    assert returned.wait(1)
    assert owner.close(timeout_ms=250) is True
    plan, response = owner.plan_for(state, candidates=[])
    assert plan is None
    assert response.error == "strategic_planner_closed"
    assert owner.memory.plan_id == ""
