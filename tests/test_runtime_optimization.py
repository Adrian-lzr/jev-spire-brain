from __future__ import annotations

import io
import urllib.error

from spirebrain.driver.decision_state import recommendation_key, state_id
from spirebrain.brain.action_broker import reconcile
from spirebrain.brain.protocol import ActionCandidate, StrategicPlan
from spirebrain.jev_brain.client import NoulSpec
from spirebrain.jev_brain.client_real import OfficialJevClient, JevApiError


def test_semantic_state_ignores_poll_noise_but_tracks_hand_and_energy():
    base = {"screen_type": "COMBAT", "current_hp": 40, "energy": 2,
            "animation_counter": 1, "timestamp": 10, "hand": ["Strike"]}
    noisy = {**base, "animation_counter": 99, "timestamp": 99,
             "uuid": "new", "trace_id": "different"}
    changed = {**base, "energy": 1}
    assert state_id(base) == state_id(noisy)
    assert recommendation_key(base) == recommendation_key(noisy)
    assert state_id(base) != state_id(changed)


def test_plan_preference_requires_candidate_signature_match():
    game = {"screen_type": "COMBAT", "energy": 1}
    old = ActionCandidate("combat:play:0:0", "play", "Strike",
                          command={"command": "play", "card": 0, "target": 0})
    new = ActionCandidate("combat:play:0:0", "play", "Defend",
                          command={"command": "play", "card": 0})
    plan = StrategicPlan("p", "state", "local", current_objective="survive",
                         preferred_candidates=[old.candidate_id])
    plan.bind_candidates([old])
    command, decision = reconcile(game=game, candidates=[new], fallback=new.command,
                                  plan=plan, state_id="new")
    assert command == new.command
    assert decision.primary_candidate is new


def test_bound_plan_does_not_avoid_unbound_id_in_a_new_state():
    game = {"screen_type": "COMBAT", "energy": 1}
    old = ActionCandidate("combat:play:0:0", "play", "Strike",
                          command={"command": "play", "card": 0, "target": 0})
    new = ActionCandidate("combat:play:0:0", "play", "Defend",
                          command={"command": "play", "card": 0})
    plan = StrategicPlan("p", "state", "local", current_objective="survive",
                         avoid_candidates=[old.candidate_id])
    # The old state had no legal avoid candidate, so bind_candidates records no
    # avoid signature. The new same-ID object must not inherit that prohibition.
    plan.bind_candidates([])
    command, decision = reconcile(game=game, candidates=[new], fallback=new.command,
                                  plan=plan, state_id="new")
    assert command == new.command
    assert decision.primary_candidate is new


def test_jev_auth_failure_is_fast_and_does_not_retry():
    class Fake(OfficialJevClient):
        def __init__(self):
            super().__init__(api_key="test", max_retries=3, backoff=0)
            self.calls_seen = 0

        def _post(self, payload):
            self.calls_seen += 1
            raise urllib.error.HTTPError("http://x", 403, "forbidden", None,
                                         io.BytesIO(b"{}"))

    client = Fake()
    try:
        client.ask("state", {"q": NoulSpec("x")})
    except JevApiError:
        pass
    else:
        raise AssertionError("expected auth error")
    assert client.calls_seen == 1
    assert client.metrics["auth_errors"] == 1


def test_defensive_posture_keeps_verified_lethal_attack(monkeypatch, tmp_path):
    from spirebrain.driver import agent as agent_module
    from spirebrain.jev_brain.decisions import Decision
    from spirebrain.driver.agent import SpireBrainAgent

    class DefensiveGate:
        def __init__(self, *args, **kwargs):
            pass

        def decide(self, *args, **kwargs):
            return Decision("combat_risk", "defensive", 1.0, False,
                             {"posture": "defensive", "reason": "test"})

    monkeypatch.setattr(agent_module, "CombatRiskGate", DefensiveGate)
    state = {
        "character": "IRONCLAD", "act": 1, "floor": 2, "current_hp": 10,
        "max_hp": 80, "screen_type": "COMBAT",
        "available_commands": ["play", "end", "wait", "state"],
        "combat": {
            "player": {"current_hp": 10, "energy": 1, "block": 0},
            "hand": [{"id": "Strike_R", "type": "ATTACK", "cost": 1,
                      "damage": 20, "has_target": True}],
            "monsters": [{"id": "JawWorm", "current_hp": 20,
                          "intent": "ATTACK", "damage": 20}],
        },
    }
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    command = agent.choose_action(state)
    assert command == {"command": "play", "card": 0, "target": 0}


def test_trace_contract_records_decision_identity_and_legality(tmp_path):
    from spirebrain.driver.agent import SpireBrainAgent
    state = {
        "character": "IRONCLAD", "screen_type": "REST", "current_hp": 20,
        "max_hp": 80, "available_commands": ["choose", "wait", "state"],
        "screen_state": {"rest_options": ["smith", "rest"]},
    }
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    agent.choose_action(state)
    record = __import__("json").loads((tmp_path / "decision_trace.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert record["event_type"] == "decision"
    assert record["decision_id"]
    assert record["state_id"]
    assert "legal" in record
    assert "decision_latency_ms" in record


def test_metrics_missing_input_is_not_success(tmp_path):
    from spirebrain.analysis.metrics import main
    assert main(["--input", str(tmp_path / "missing.jsonl")]) == 2
