"""Focused S4 run/lifecycle regressions.

These tests exercise the owner/worker boundary without a real provider or game
installation.  They intentionally use tiny state dictionaries so a late
result can be made deterministic and inspected directly.
"""

from __future__ import annotations

import threading
import time

from spirebrain.brain.memory import RunMemory
from spirebrain.brain.orchestrator import StrategicOrchestrator
from spirebrain.brain.planner import stable_state_id
from spirebrain.brain.protocol import (
    BrainResponse,
    DecisionContext,
    DecisionRequest,
    RunSession,
    StrategicPlan,
)
from spirebrain.runtime_budget import DecisionBudget


def _state(run_id: str = "run-a", screen: str = "EVENT", floor: int = 1) -> dict:
    return {
        "run_id": run_id,
        "seed": "seed-1",
        "screen_type": screen,
        "act": 1,
        "floor": floor,
        "current_hp": 60,
        "max_hp": 80,
        "screen_state": {"options": ["left", "right"]},
        "available_commands": ["choose", "wait", "state"],
    }


def _plan(payload: dict, plan_id: str) -> StrategicPlan:
    state_id = stable_state_id(payload["state"] if "state" in payload else payload)
    return StrategicPlan.from_dict(
        {
            "plan_id": plan_id,
            "state_id": payload.get("state_id", state_id),
            "run_id": payload["run_id"],
            "current_objective": plan_id,
            "long_term_goal": "finish",
            "priority": [],
            "preferred_candidates": [],
            "avoid_candidates": [],
            "resource_constraints": {},
            "next_steps": [],
            "replan_triggers": [],
            "reason": "fixture",
            "uncertainty": "",
            "expires_after": 3,
        },
        state_id=payload.get("state_id", state_id),
        run_id=payload["run_id"],
    )


def test_a01_a_b_a_only_latest_generation_can_commit():
    """Returning to A is a new request; B's late result cannot win."""
    started = threading.Event()
    release = threading.Event()

    class Provider:
        backend_name = "controlled"
        async_required = True

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            call = self.calls
            if call == 1:
                started.set()
                assert release.wait(1)
            return BrainResponse(plan=_plan(payload, f"p{call}"), backend=self.backend_name)

    provider = Provider()
    owner = StrategicOrchestrator(client=provider, backend="controlled")
    state_a = _state(floor=1)
    state_b = _state(floor=2)
    owner.plan_for(state_a, candidates=[])
    assert started.wait(1)
    owner.plan_for(state_b, candidates=[])
    owner.plan_for(state_a, candidates=[])
    release.set()

    deadline = time.monotonic() + 1
    accepted = None
    while time.monotonic() < deadline:
        accepted, _ = owner.plan_for(state_a, candidates=[])
        if accepted is not None:
            break
        time.sleep(0.005)
    assert accepted is not None
    assert accepted.plan_id == "p2"
    assert owner.memory.plan_id == "p2"


def test_a02_same_seed_after_terminal_is_a_new_memory_epoch():
    memory = RunMemory()
    memory.observe(_state("same", "COMBAT"), state_id="old")
    memory.record_observed_action({"label": "old action"}, state_id="old")
    epoch = memory.run_epoch
    memory.observe(_state("same", "DEATH"), state_id="death")
    memory.observe(_state("same", "COMBAT"), state_id="new")
    assert memory.run_epoch > epoch
    assert all(event.get("state_id") not in {"old", "death"} for event in memory.events)
    assert not memory.deviations


def test_a03_deviation_snapshot_is_carried_to_next_worker_input():
    owner = RunMemory()
    owner.observe(_state(), state_id="s1")
    owner.record_deviation("choose left", "choose right", state_id="s1",
                           decision_id="d1", evidence={"source": "fixture"})
    snapshot = owner.snapshot()
    worker = RunMemory()
    new = worker.sync_from_snapshot(snapshot)
    assert new and new[0]["actual"] == "choose right"
    assert worker.digest()["deviations"] == owner.digest()["deviations"]


def test_a04_late_result_after_reset_is_dropped():
    started = threading.Event()
    release = threading.Event()

    class Provider:
        backend_name = "controlled"
        async_required = True

        def plan(self, payload):
            started.set()
            assert release.wait(1)
            return BrainResponse(plan=_plan(payload, "old"), backend=self.backend_name)

    owner = StrategicOrchestrator(client=Provider(), backend="controlled")
    state = _state("same-run")
    owner.plan_for(state, candidates=[])
    assert started.wait(1)
    owner.reset("same-run")
    release.set()
    time.sleep(0.02)
    owner.plan_for(state, candidates=[])
    assert owner.memory.plan_id == ""
    assert owner.expired_plan_results >= 1 or owner.invalidated_plan_requests >= 1


def test_a05_zero_budget_prevents_provider_and_marks_fallback():
    class Provider:
        backend_name = "spy"
        def __init__(self):
            self.calls = 0
        def plan(self, payload):
            self.calls += 1
            return BrainResponse(plan=_plan(payload, "unexpected"), backend=self.backend_name)

    provider = Provider()
    owner = StrategicOrchestrator(client=provider, backend="spy", async_planning=False)
    plan, response = owner.plan_for(_state(), candidates=[],
                                    budget=DecisionBudget(total_ms=1000, max_calls=0))
    assert plan is None
    assert provider.calls == 0
    assert response.fallback is True


def test_a06_choose_does_not_enter_a_second_planning_path():
    class Provider:
        backend_name = "spy"
        def __init__(self):
            self.calls = 0
        def plan(self, payload):
            self.calls += 1
            return BrainResponse(error="offline", fallback=True)

    provider = Provider()
    owner = StrategicOrchestrator(client=provider, backend="spy", async_planning=False)
    state = _state()
    owner.plan_for(state, candidates=[])
    owner.choose(state, {"command": "state"}, candidates=[])
    assert provider.calls == 1


def test_a07_decision_request_isolated_from_live_state_and_plan():
    session = RunSession(run_id="r", run_epoch=2, seed="s")
    state = {"hand": [{"id": "Strike"}]}
    plan = {"objective": ["survive"]}
    context = DecisionContext(session, state_id="s1", decision_id="d1", state=state)
    request = DecisionRequest(context=context, state=state, plan=plan)
    state["hand"][0]["id"] = "Defend"
    plan["objective"].append("save_gold")
    assert request.state["hand"][0]["id"] == "Strike"
    assert request.plan["objective"] == ["survive"]
    assert request.context.state["hand"][0]["id"] == "Strike"


def test_a08_owner_commit_copies_worker_plan_and_rebinds_state():
    owner = StrategicOrchestrator(client=None, backend="none", async_planning=False)
    state = _state()
    owner.memory.observe(state, state_id=stable_state_id(state))
    plan = _plan({"state": state, "run_id": owner.memory.run_id,
                  "state_id": stable_state_id(state)}, "worker")
    assert owner.commit_worker_plan(plan, state, candidates=[])
    assert owner.current_plan is not plan
    assert owner.memory.plan_id == "worker"
    plan.current_objective = "mutated after commit"
    assert owner.current_plan.current_objective == "worker"
