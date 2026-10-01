"""Replay the safety boundaries of the in-game advisor without a running game."""
import io
import json
import threading

from spirebrain.driver.advisor import AdviseSession
from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.legality import check_action
from spirebrain.driver.live_state import normalize_game_state
from spirebrain.driver.trace import DecisionTrace
from spirebrain.driver.decision_state import state_id as semantic_state_id
from spirebrain.guide.rules import GuideBook
from spirebrain.tactical.combat_greedy import recommend_action


def game(**changes):
    return dict({"character": "IRONCLAD", "act": 1, "floor": 1,
                 "current_hp": 20, "max_hp": 80, "deck": [],
                 "screen_type": "REST", "available_commands": ["choose", "wait", "state"],
                 "screen_state": {"rest_options": ["smith", "rest"]}}, **changes)


def test_hard_rest_rule_overrides_model_and_preserves_index(tmp_path):
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    state = game()
    assert agent.choose_action(state) == {"command": "choose", "choice": 1}
    assert agent.jev.calls == 0
    assert agent.history[-1]["detail"]["source_type"] == "guide_rule"
    assert check_action(state, agent.history[-1]["command"])[0]


def test_other_characters_do_not_inherit_ironclad_policy(tmp_path):
    state = game(character="THE_SILENT")
    assert GuideBook().evaluate(state, 0).matches == []
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    assert agent.quick_advice(state)["status"] == "unsupported"


def test_all_lethal_event_options_do_not_fall_back_to_first(tmp_path):
    state = game(screen_type="EVENT", screen_state={"options": [
        {"label": "Lose health", "hp_cost": 20}, {"label": "Locked", "disabled": True}]})
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    command = agent.choose_action(state)
    assert command["command"] == "state"
    assert not check_action(state, command)[0]
    assert agent.jev.calls == 0


def test_live_combat_alias_does_not_overwrite_grid_screen():
    combat = {"player": {"energy": 1}, "hand": []}
    state = {"class": "IRONCLAD", "screen_type": "NONE", "room_phase": "COMBAT",
             "combat_state": combat}
    normalized = normalize_game_state(state)
    assert normalized["screen_type"] == "COMBAT"
    assert normalized["combat"] == combat
    assert state["screen_type"] == "NONE"
    assert normalize_game_state(dict(state, screen_type="GRID"))["screen_type"] == "GRID"


def combat_game():
    return game(screen_type="COMBAT", available_commands=["play", "end", "potion", "wait"],
                combat={"player": {"energy": 2, "current_hp": 20, "block": 0},
                        "hand": [{"id": "Strike_R", "type": "ATTACK", "cost": 1,
                                  "damage": 6, "has_target": True}],
                        "monsters": [{"current_hp": 0, "is_gone": True},
                                     {"current_hp": 5, "intent": "ATTACK", "damage": 8}]})


def test_live_map_uses_explicit_reachable_nodes():
    full_map = [{"x": 0, "y": 1}, {"x": 2, "y": 1}]
    state = game(screen_type="MAP", map=full_map,
                 screen_state={"next_nodes": [full_map[1]]})
    normalized = normalize_game_state(state)
    assert normalized["map"]["next_nodes"] == [full_map[1]]
    assert normalized["full_map"] == full_map


def test_target_uses_live_enemy_index_and_rejects_dead_target():
    state = combat_game()
    advice = recommend_action(state)
    assert advice.command == {"command": "play", "card": 0, "target": 1}
    assert check_action(state, advice.command)[0]
    assert not check_action(state, dict(advice.command, target=0))[0]


def test_dangerous_turn_can_recommend_usable_healing_potion():
    state = combat_game()
    state["potions"] = [{"id": "BloodPotion", "can_use": True}]
    advice = recommend_action(state)
    assert advice.command == {"command": "potion", "action": "use", "slot": 0}
    assert check_action(state, advice.command)[0]


def test_targeted_potion_requires_live_target():
    state = combat_game()
    state["potions"] = [{"id": "FirePotion", "can_use": True, "requires_target": True}]
    assert not check_action(state, {"command": "potion", "slot": 0, "target": 0})[0]
    assert check_action(state, {"command": "potion", "slot": 0, "target": 1})[0]


def test_unknown_damage_does_not_claim_exact_kill():
    state = combat_game()
    del state["combat"]["hand"][0]["damage"]
    advice = recommend_action(state)
    assert advice.uncertain
    assert "击败" not in advice.reason


class Feed:
    def __init__(self):
        self.events = []

    def publish(self, kind, data):
        self.events.append((kind, data))


class SlowAgent:
    def __init__(self):
        self.feed = Feed()
        self.history = []
        self.started = threading.Event()
        self.release = threading.Event()

    def clone_for_advice(self):
        return self

    def quick_advice(self, payload):
        return {"status": "thinking"}

    def choose_action(self, payload):
        self.started.set()
        assert self.release.wait(3)
        return {"command": "choose", "choice": 0}


def test_slow_model_does_not_block_poll_and_late_result_is_dropped(tmp_path):
    agent = SlowAgent()
    agent.trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="test")
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                           warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
                           fingerprint=lambda g: json.dumps(g, sort_keys=True), message_count=lambda: 1)
    state = game()
    assert session.advise(state, ["choose", "wait"], True) == "wait 20"
    expired_decision_id = session._current_decision_id
    assert agent.started.wait(1)
    # Player advances to an unknown screen before the model finishes.
    changed = game(screen_type="UNKNOWN")
    assert session.advise(changed, ["wait"], False) == "wait 20"
    agent.release.set()
    result = session._completed.get(timeout=2)
    session._completed.put(result)
    session.advise(changed, ["wait"], False)
    assert agent.feed.events[-1][1]["status"] == "unavailable"
    assert not any(data.get("status") == "ready" for _, data in agent.feed.events)
    expired = agent.trace.find_decision(expired_decision_id)["expired"]
    assert len(expired) == 1
    assert expired[0]["fallback_reason"] == "state_changed"
    assert expired[0]["state_id"] != session._latest_state_id


def test_advisor_completion_queue_is_bounded_and_latest_wins(tmp_path):
    agent = SlowAgent()
    agent.trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="test")
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                           warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
                           fingerprint=lambda g: json.dumps(g, sort_keys=True), message_count=lambda: 1)

    old_payload = {"decision_id": "decision-old", "run_id": "run-a"}
    old_entry = {"decision_id": "decision-old", "state_id": "state-old",
                 "request_id": "brain-old",
                 "detail": {"provider_request_ids": ["jev-old"]}}
    session._enqueue_completed(("old", 1, "run-a", 1, old_payload,
                                {"command": "end"}, old_entry, None, None))
    session._enqueue_completed(("new", 2, "run-a", 1, {}, None, None, None, None))

    assert session._completed.maxsize == 1
    assert session._completed.get_nowait()[0] == "new"
    assert session.dropped_completions == 1
    expired = agent.trace.find_decision("decision-old")["expired"]
    assert {event["request_id"] for event in expired} == {"jev-old"}
    assert all(event["fallback_reason"] == "completion_queue_replaced" for event in expired)


def test_expired_completion_does_not_attribute_reused_brain_request(tmp_path):
    agent = SlowAgent()
    agent.trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="test")
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                            warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
                            fingerprint=lambda g: json.dumps(g, sort_keys=True), message_count=lambda: 1)
    payload = {"decision_id": "decision-current", "run_id": "run-a"}
    entry = {"decision_id": "decision-current", "state_id": "state-current",
             "request_id": "legacy-brain-request",
             "detail": {"brain_request_id": "reused-old-brain-request"}}

    session._record_expired_completion(
        ("fingerprint", 4, "run-a", 1, payload, {"command": "state"}, entry, None),
        "stale_state",
    )

    expired = agent.trace.find_decision("decision-current")["expired"]
    assert len(expired) == 1
    assert expired[0]["request_id"] is None
    assert expired[0]["provider_request_ids"] == []


def test_typed_worker_context_mismatch_is_expired_not_committed(tmp_path):
    from spirebrain.brain.protocol import (
        AdviceWorkerResult, DecisionContext, DecisionRequest, RunSessionSnapshot,
    )

    agent = SlowAgent()
    agent.trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="test")
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                            warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
                            fingerprint=lambda g: "fp", message_count=lambda: 1)
    payload = game(run_id="run-a", state_id="state-a")
    context = DecisionContext(
        run=RunSessionSnapshot(run_id="run-b", run_epoch=0),
        state_id=semantic_state_id(payload), decision_id="decision-a",
        generation=1, state=payload,
    )
    request = DecisionRequest(context=context, state=payload)
    proposal = AdviceWorkerResult(
        request=request, command={"command": "choose", "choice": 0},
        history_entry={"decision_id": "decision-a", "state_id": context.state_id,
                       "run_id": "run-b", "run_epoch": 0,
                       "detail": {"provider_request_ids": ["request-a"]}},
    )
    session._latest_fp = "fp"
    session._latest_state_id = context.state_id
    session._generation = 1
    session._completed.put(("fp", 1, "run-a", 0, payload,
                            proposal.command, proposal.history_entry, None, proposal))

    session._drain_completed()

    assert session.tracker.pending is None
    expired = agent.trace.find_decision("decision-a")["expired"]
    assert len(expired) == 1
    assert expired[0]["fallback_reason"] == "worker_identity_mismatch"
    assert expired[0]["request_id"] == "request-a"


def test_advisor_replaced_pending_work_is_traced_as_skipped(tmp_path):
    class Agent:
        def __init__(self):
            self.trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="test")
            self.feed = Feed()

        def quick_advice(self, payload):
            return {"status": "thinking"}

    agent = Agent()
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                            warn_stream=io.StringIO(), ensure_offered=lambda *a, **k: "wait 20",
                            fingerprint=lambda state: str(state.get("state_id")),
                            message_count=lambda: 1)
    session._work_running = True  # Hold the worker so both requests stay queued.
    first = game(screen_type="EVENT", state_id="state-a", run_id="run-a")
    session._current_decision_id = "decision-a"
    session._generation = 1
    session._advise_async(first, ["choose", "wait"], "fp-a")
    second = game(screen_type="EVENT", state_id="state-b", run_id="run-a")
    session._current_decision_id = "decision-b"
    session._generation = 2
    session._advise_async(second, ["choose", "wait"], "fp-b")

    chain = agent.trace.find_decision("decision-a")
    assert len(chain["skipped"]) == 1
    assert chain["skipped"][0]["reason"] == "superseded_before_start"
    assert chain["skipped"][0]["state_id"] == semantic_state_id(first)
    session.close()


def test_late_result_expiry_links_original_jev_and_brain_requests(tmp_path):
    class CorrelatedSlowAgent(SlowAgent):
        def choose_action(self, payload):
            self.started.set()
            assert self.release.wait(3)
            decision_id = payload["decision_id"]
            state_id = semantic_state_id(payload)
            self.trace.record("provider_request", {
                "run_id": "", "run_epoch": 0, "decision_id": decision_id,
                "state_id": state_id, "request_id": "jev-request",
                "logical_request_id": "jev-request", "attempt_id": "jev-request:1",
                "provider": "fixture-jev",
            })
            self.trace.record("provider_response", {
                "run_id": "", "run_epoch": 0, "decision_id": decision_id,
                "state_id": state_id, "request_id": "jev-request",
                "logical_request_id": "jev-request", "attempt_id": "jev-request:1",
                "provider": "fixture-jev", "latency_ms": 1,
                "error_kind": "", "fallback": False,
            })
            self.history.append({
                "decision_id": decision_id, "state_id": state_id,
                "run_id": "", "run_epoch": 0, "request_id": "brain-request",
                "detail": {"brain_request_id": "brain-request",
                           "provider_request_ids": ["jev-request"]},
                "command": {"command": "choose", "choice": 0},
            })
            return {"command": "choose", "choice": 0}

    agent = CorrelatedSlowAgent()
    agent.trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="test")
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                            warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
                            fingerprint=lambda state: json.dumps(state, sort_keys=True),
                            message_count=lambda: 1)
    assert session.advise(game(), ["choose", "wait"], True) == "wait 20"
    old_decision_id = session._current_decision_id
    assert agent.started.wait(1)
    changed = game(screen_type="UNKNOWN")
    session.advise(changed, ["wait"], False)
    agent.release.set()
    completed = session._completed.get(timeout=2)
    session._completed.put(completed)
    session._drain_completed()

    chain = agent.trace.find_decision(old_decision_id)
    request_ids = {event["request_id"] for event in chain["requests"]}
    expired_by_request = {event.get("request_id"): event for event in chain["expired"]}
    assert request_ids == {"jev-request"}
    assert "jev-request" in expired_by_request
    assert expired_by_request["jev-request"]["decision_id"] == old_decision_id
    assert expired_by_request["jev-request"]["state_id"] == chain["requests"][0]["state_id"]


def test_advisor_close_is_bounded_and_invalidates_late_completion():
    agent = SlowAgent()
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                           warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
                           fingerprint=lambda g: json.dumps(g, sort_keys=True), message_count=lambda: 1)
    assert session.advise(game(), ["choose", "wait"], True) == "wait 20"
    assert agent.started.wait(1)

    assert session.close(timeout_ms=5) is False
    agent.release.set()
    assert session.close(timeout_ms=250) is True
    session._drain_completed()
    assert not any(data.get("status") == "ready" for _, data in agent.feed.events)


def test_advice_worker_uses_the_owner_run_epoch(tmp_path):
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    agent.begin_new_run("run-identity", seed="same-seed")
    agent.strategic.memory.record_deviation(
        "old-advice", "observed-action", state_id="prior-state",
        decision_id="prior-decision", evidence={"source": "test"},
    )
    state = combat_game()
    state.update(run_id="run-identity", seed="same-seed")
    available = ["play", "end", "wait", "state"]
    session = AdviseSession(
        agent, advice_path=None, poll_frames=20,
        warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
        fingerprint=lambda g: json.dumps(g, sort_keys=True), message_count=lambda: 1,
    )

    assert session.advise(state, available, True) == "wait 20"
    worker = session._worker_agent
    assert worker is not None
    thread = session._worker_thread
    if thread is not None:
        thread.join(timeout=2)
    session._drain_completed()

    assert worker.history
    assert worker.history[-1]["run_epoch"] == agent.run_session.run_epoch
    assert worker.strategic.memory.run_epoch == agent.strategic.memory.run_epoch
    assert worker.strategic.memory.digest()["deviations"] == agent.strategic.memory.digest()["deviations"]
    assert session.tracker.pending is not None
    assert session.tracker.pending.run_epoch == agent.run_session.run_epoch
    session.close()


def test_advice_worker_adopts_owner_memory_but_never_starts_strategy_requests(tmp_path):
    from spirebrain.brain.protocol import BrainResponse

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    agent.begin_new_run("run-memory", seed="same-seed")
    worker = agent.clone_for_advice()

    worker.adopt_run_snapshot(agent.run_session.snapshot(), agent.strategic.memory.snapshot())
    worker.strategic.plan_valid = True
    agent.strategic.memory.record_deviation(
        "combat:play:0:1", "end turn", state_id="state-a",
        decision_id="decision-a", evidence={"screen": "COMBAT"},
        observed_state={"screen_type": "COMBAT", "energy": 0},
    )

    worker.adopt_run_snapshot(agent.run_session.snapshot(), agent.strategic.memory.snapshot())

    worker_memory = worker.strategic.memory.digest()
    owner_memory = agent.strategic.memory.digest()
    assert worker_memory["deviations"] == owner_memory["deviations"]
    assert worker_memory["recent_events"][-1]["event_type"] == "deviation_record"
    assert worker.strategic.plan_valid is False

    calls = []

    class CaptureProvider:
        backend_name = "capture"

        def plan(self, payload):
            calls.append(payload)
            return BrainResponse(backend=self.backend_name, error="offline fixture",
                                 fallback=True)

    worker.strategic.planner.provider = CaptureProvider()
    state = combat_game()
    state.update(run_id="run-memory", seed="same-seed")
    worker.choose_action(state)

    assert not calls
    assert agent.strategic.memory.digest() == owner_memory


def test_advice_worker_plan_is_committed_by_owner_after_current_result(tmp_path):
    from spirebrain.brain.planner import stable_state_id
    from spirebrain.brain.protocol import AdviceWorkerResult, BrainResponse, StrategicPlan

    class Provider:
        backend_name = "mock"
        async_required = False

        def plan(self, payload):
            state_id = stable_state_id(payload["state"])
            plan = StrategicPlan.from_dict({
                "plan_id": "worker-plan", "state_id": state_id,
                "run_id": payload["run_id"], "current_objective": "保留能量",
                "long_term_goal": "稳定通过当前战斗", "priority": ["survive"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": ["观察敌方意图"],
                "replan_triggers": ["enemy_changed"], "reason": "固定回放响应",
                "uncertainty": "", "expires_after": 2,
            }, state_id=state_id, run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend=self.backend_name,
                                 request_id="fixed-request")

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=Provider(), async_planning=False,
                            log_dir=tmp_path)
    agent.begin_new_run("run-plan", seed="seed-plan")
    state = combat_game()
    state.update(run_id="run-plan", seed="seed-plan")
    available = ["play", "end", "wait", "state"]
    session = AdviseSession(
        agent, advice_path=None, poll_frames=20,
        warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
        fingerprint=lambda g: json.dumps(g, sort_keys=True), message_count=lambda: 1,
    )

    assert session.advise(state, available, True) == "wait 20"
    worker = session._worker_agent
    assert worker is not None
    thread = session._worker_thread
    if thread is not None:
        thread.join(timeout=2)
    completed = session._completed.get_nowait()
    assert isinstance(completed[8], AdviceWorkerResult)
    assert completed[8].request.context.decision_id == session._current_decision_id
    assert completed[8].request.context.run_id == "run-plan"
    session._completed.put(completed)
    session._drain_completed()

    assert session.tracker.pending is not None
    assert session.tracker.pending.plan_id == "worker-plan"
    assert agent.strategic.plan_valid is True
    assert agent.strategic.current_plan.plan_id == "worker-plan"
    assert agent.strategic.memory.plan_id == "worker-plan"
    assert agent.strategic.current_plan is not worker.strategic.current_plan
    session.close()


def test_advisor_owns_phase_plan_request_across_tactical_state_changes(tmp_path):
    from spirebrain.brain.protocol import BrainResponse, StrategicPlan

    started = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    class SlowPhaseProvider:
        backend_name = "phase-owner"
        async_required = True

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            started.set()
            assert release.wait(2)
            plan = StrategicPlan.from_dict({
                "plan_id": "combat-phase-plan",
                "state_id": payload["state_id"],
                "run_id": payload["run_id"],
                "current_objective": "优先处理当前遭遇威胁",
                "long_term_goal": "保留生命并维持铁甲构筑方向",
                "priority": ["survive"], "preferred_candidates": [],
                "avoid_candidates": [], "resource_constraints": {},
                "next_steps": ["按当前合法候选评估行动"],
                "replan_triggers": ["enemy_changed"],
                "reason": "阶段服务固定响应", "uncertainty": "",
                "expires_after": 3,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            returned.set()
            return BrainResponse(plan=plan, backend=self.backend_name,
                                 request_id="phase-request-1")

    provider = SlowPhaseProvider()
    agent = SpireBrainAgent(
        jev_backend="mock", brain_backend="scripted", brain_client=provider,
        async_planning=True, log_dir=tmp_path,
    )
    agent.begin_new_run("run-phase-owner", seed="fixed-seed")
    session = AdviseSession(
        agent, advice_path=None, poll_frames=20,
        warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
        fingerprint=lambda state: json.dumps(state, sort_keys=True),
        message_count=lambda: 1,
    )
    available = ["play", "end", "potion", "wait", "state"]
    latest = combat_game()
    latest.update(run_id="run-phase-owner", seed="fixed-seed")

    assert session.advise(latest, available, True) == "wait 20"
    assert started.wait(1)
    for energy in (1, 0):
        latest = json.loads(json.dumps(latest))
        latest["combat"]["player"]["energy"] = energy
        latest["combat"]["hand"][0]["damage"] += 1
        assert session.advise(latest, available, True) == "wait 20"

    release.set()
    assert returned.wait(1)
    plan_thread = agent.strategic._plan_thread
    if plan_thread is not None:
        plan_thread.join(timeout=2)

    # A same-phase plan completion must refresh the latest state, not replay
    # the action computed for the first hand/energy snapshot.
    assert session.advise(latest, available, True) == "wait 20"
    worker_thread = session._worker_thread
    if worker_thread is not None:
        worker_thread.join(timeout=2)
    session._drain_completed()

    assert provider.calls == 1
    assert agent.strategic.current_plan is not None
    assert agent.strategic.current_plan.plan_id == "combat-phase-plan"
    assert session.tracker.pending is not None
    assert session.tracker.pending.state_id == semantic_state_id(latest)
    assert session.tracker.pending.plan_id == "combat-phase-plan"
    assert session.tracker.pending.command in ({"command": "end"},
                                               {"command": "play", "card": 0, "target": 1})
    session.close()


def test_stale_advice_action_is_dropped_but_same_act_plan_is_rebound(tmp_path):
    from spirebrain.brain.action_broker import build_action_candidates
    from spirebrain.brain.protocol import (
        AdviceWorkerResult, DecisionContext, DecisionRequest,
        RunSessionSnapshot, StrategicPlan,
    )
    from spirebrain.driver.live_state import GameSnapshot

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none",
                            log_dir=tmp_path)
    agent.begin_new_run("run-late-plan", seed="fixed-seed")
    source = combat_game()
    source.update(run_id="run-late-plan", seed="fixed-seed")
    source_snapshot = GameSnapshot.from_communication(source)
    source_candidates = build_action_candidates(source_snapshot.payload)
    preferred_id = source_candidates[0].candidate_id
    plan = StrategicPlan.from_dict({
        "plan_id": "late-phase-plan", "state_id": source_snapshot.state_id,
        "run_id": "run-late-plan", "current_objective": "保留生命",
        "long_term_goal": "稳定通过当前幕", "priority": ["survive"],
        "preferred_candidates": [preferred_id], "avoid_candidates": [],
        "resource_constraints": {}, "next_steps": ["重新评估当前威胁"],
        "replan_triggers": ["enemy_changed"], "reason": "固定响应",
        "uncertainty": "", "expires_after": 2,
    }, state_id=source_snapshot.state_id, run_id="run-late-plan")
    plan.bind_candidates(source_candidates)

    current = json.loads(json.dumps(source))
    current["combat"]["hand"][0]["id"] = "Bash"
    current["combat"]["hand"][0]["damage"] = 8
    current_snapshot = GameSnapshot.from_communication(current)
    assert current_snapshot.state_id != source_snapshot.state_id
    assert (build_action_candidates(current_snapshot.payload)[0].candidate_signature
            != source_candidates[0].candidate_signature)

    session = AdviseSession(
        agent, advice_path=None, poll_frames=20,
        warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
        fingerprint=lambda value: json.dumps(value, sort_keys=True),
        message_count=lambda: 1,
    )
    epoch = agent.run_session.run_epoch
    session._generation = 2
    session._latest_fp = "state-b"
    session._latest_state_id = current_snapshot.state_id
    session._latest_game = dict(current, available_commands=current["available_commands"])

    request = DecisionRequest(
        context=DecisionContext(
            run=RunSessionSnapshot(run_id="run-late-plan", run_epoch=epoch,
                                   seed="fixed-seed", mode="advise"),
            state_id=source_snapshot.state_id, decision_id="decision-a",
            generation=1, state=source,
        ),
        state=source,
    )
    proposal = AdviceWorkerResult(
        request=request, command={"command": "play", "card": 0, "target": 1},
        plan=plan,
    )
    session._completed.put((
        "state-a", 1, "run-late-plan", epoch, source, proposal.command,
        None, None, proposal,
    ))
    session._drain_completed()

    assert session.tracker.pending is None
    assert agent.strategic.plan_valid is True
    assert agent.strategic.current_plan.plan_id == "late-phase-plan"
    assert agent.strategic.current_plan.state_id == current_snapshot.state_id
    assert agent.strategic.current_plan.preferred_candidates == []
    assert agent.strategic.current_plan.candidate_bindings == {}
    assert session.dropped_completions == 0
    expired = agent.trace.find_decision("decision-a")["expired"]
    assert len(expired) == 1
    assert expired[0]["fallback_reason"] == "generation_mismatch"
    session.close()
