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
