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
from spirebrain.brain.protocol import BrainResponse, StrategicPlan
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
