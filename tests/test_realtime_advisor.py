"""Replay the safety boundaries of the in-game advisor without a running game."""
import io
import json
import threading

from spirebrain.driver.advisor import AdviseSession
from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.legality import check_action
from spirebrain.driver.live_state import normalize_game_state
from spirebrain.guide.rules import GuideBook
from spirebrain.tactical.combat_greedy import recommend_action


def game(**changes):
    return dict({"character": "IRONCLAD", "act": 1, "floor": 1,
                 "current_hp": 20, "max_hp": 80, "deck": [],
                 "screen_type": "REST", "available_commands": ["choose", "wait", "state"],
                 "screen_state": {"rest_options": ["smith", "rest"]}}, **changes)


def test_hard_rest_rule_overrides_model_and_preserves_index(tmp_path):
    agent = SpireBrainAgent(jev_backend="mock", log_dir=tmp_path)
    state = game()
    assert agent.choose_action(state) == {"command": "choose", "choice": 1}
    assert agent.jev.calls == 0
    assert agent.history[-1]["detail"]["source_type"] == "guide_rule"
    assert check_action(state, agent.history[-1]["command"])[0]


def test_other_characters_do_not_inherit_ironclad_policy(tmp_path):
    state = game(character="THE_SILENT")
    assert GuideBook().evaluate(state, 0).matches == []
    agent = SpireBrainAgent(jev_backend="mock", log_dir=tmp_path)
    assert agent.quick_advice(state)["status"] == "unsupported"


def test_all_lethal_event_options_do_not_fall_back_to_first(tmp_path):
    state = game(screen_type="EVENT", screen_state={"options": [
        {"label": "Lose health", "hp_cost": 20}, {"label": "Locked", "disabled": True}]})
    agent = SpireBrainAgent(jev_backend="mock", log_dir=tmp_path)
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


def test_slow_model_does_not_block_poll_and_late_result_is_dropped():
    agent = SlowAgent()
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                           warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
                           fingerprint=lambda g: json.dumps(g, sort_keys=True), message_count=lambda: 1)
    state = game()
    assert session.advise(state, ["choose", "wait"], True) == "wait 20"
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


def test_advisor_completion_queue_is_bounded_and_latest_wins():
    agent = SlowAgent()
    session = AdviseSession(agent, advice_path=None, poll_frames=20,
                           warn_stream=io.StringIO(), ensure_offered=lambda line, *a, **k: line,
                           fingerprint=lambda g: json.dumps(g, sort_keys=True), message_count=lambda: 1)

    session._enqueue_completed(("old", 1, 0, {}, None, None, None))
    session._enqueue_completed(("new", 2, 0, {}, None, None, None))

    assert session._completed.maxsize == 1
    assert session._completed.get_nowait()[0] == "new"
    assert session.dropped_completions == 1


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


def test_advice_worker_adopts_owner_memory_and_invalidates_plan_after_deviation(tmp_path):
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

    assert calls
    assert any(event["event_type"] == "deviation_record"
               for event in calls[0]["memory"]["recent_events"])
    assert agent.strategic.memory.digest() == owner_memory


def test_advice_worker_plan_is_committed_by_owner_after_current_result(tmp_path):
    from spirebrain.brain.planner import stable_state_id
    from spirebrain.brain.protocol import BrainResponse, StrategicPlan

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
    session._drain_completed()

    assert session.tracker.pending is not None
    assert session.tracker.pending.plan_id == "worker-plan"
    assert agent.strategic.plan_valid is True
    assert agent.strategic.current_plan.plan_id == "worker-plan"
    assert agent.strategic.memory.plan_id == "worker-plan"
    assert agent.strategic.current_plan is not worker.strategic.current_plan
    session.close()
