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
