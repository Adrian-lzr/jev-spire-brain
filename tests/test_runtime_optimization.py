from __future__ import annotations

import io
import json
import urllib.error

from spirebrain.driver.decision_state import recommendation_key, state_id
from spirebrain.brain.action_broker import build_action_candidates, reconcile
from spirebrain.brain.protocol import (
    ActionCandidate,
    DecisionContext,
    DecisionProposal,
    FinalDecision,
    RunSession,
    RunSessionSnapshot,
    StrategicPlan,
)
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


def test_bound_plan_does_not_avoid_same_id_when_entity_signature_changed():
    """An avoid preference is state-bound just like a preferred preference."""
    game = {"screen_type": "COMBAT", "energy": 1}
    old = ActionCandidate("combat:play:0:0", "play", "Strike",
                          command={"command": "play", "card": 0, "target": 0},
                          entity_id="card:Strike:slot:0")
    new = ActionCandidate("combat:play:0:0", "play", "Defend",
                          command={"command": "play", "card": 0},
                          entity_id="card:Defend:slot:0")
    plan = StrategicPlan("p", "state", "local", current_objective="survive",
                         avoid_candidates=[old.candidate_id])
    plan.bind_candidates([old])
    command, decision = reconcile(game=game, candidates=[new], fallback=new.command,
                                  plan=plan, state_id="new")
    assert command == new.command
    assert decision.primary_candidate is new


def test_candidate_signatures_include_duplicate_card_upgrade_and_target_identity():
    state = {
        "screen_type": "COMBAT", "available_commands": ["play", "end"],
        "combat": {
            "player": {"energy": 2, "current_hp": 50, "block": 0},
            "hand": [
                {"id": "Strike_R", "name": "Strike", "type": "ATTACK",
                 "cost": 1, "damage": 6, "upgrades": 0, "has_target": True},
                {"id": "Strike_R", "name": "Strike", "type": "ATTACK",
                 "cost": 1, "damage": 9, "upgrades": 1, "has_target": True},
            ],
            "monsters": [
                {"id": "Cultist", "name": "Cultist", "current_hp": 20},
                {"id": "Cultist", "name": "Cultist", "current_hp": 20,
                 "instance_id": "cultist-b"},
            ],
        },
    }
    state["combat"]["monsters"][0]["instance_id"] = "cultist-a"
    candidates = [c for c in build_action_candidates(state) if c.kind == "play"]
    assert len({c.candidate_signature for c in candidates}) == len(candidates)
    assert {c.upgrade_state for c in candidates} >= {"+0", "+1"}
    assert len({c.validity["target_entity_id"] for c in candidates}) == 2
    reordered = dict(state)
    reordered["combat"] = dict(state["combat"], monsters=list(reversed(state["combat"]["monsters"])))
    reordered_candidates = [c for c in build_action_candidates(reordered) if c.kind == "play"]
    assert {c.candidate_id: c.candidate_signature for c in candidates} != {
        c.candidate_id: c.candidate_signature for c in reordered_candidates}


def test_final_decision_captures_run_identity_instead_of_mutable_session():
    session = RunSession(run_id="run-a", run_epoch=2, seed="seed-a")
    context = DecisionContext(run=session, state_id="s1", decision_id="d1")
    final = FinalDecision(
        context=context, command={"command": "wait"},
        proposal=DecisionProposal(point="combat", command={"command": "wait"}),
        source_type="rule_fallback", reason="unknown",
    )
    session.observe("run-b")
    assert isinstance(final.context.run, RunSessionSnapshot)
    assert final.run_id == "run-a"
    assert final.context.run_epoch == 2
    assert final.context.matches(run_id="run-a", run_epoch=2, state_id="s1")


def test_defensive_gate_uses_structured_lethal_fact_not_reason_text(monkeypatch, tmp_path):
    from spirebrain.driver import agent as agent_module
    from spirebrain.jev_brain.decisions import Decision
    from spirebrain.tactical.combat_greedy import ActionSuggestion
    from spirebrain.driver.agent import SpireBrainAgent

    class DefensiveGate:
        def __init__(self, *args, **kwargs):
            pass

        def decide(self, *args, **kwargs):
            return Decision("combat_risk", "defensive", 1.0, False,
                             {"posture": "defensive", "reason": "test"})

    monkeypatch.setattr(agent_module, "CombatRiskGate", DefensiveGate)
    monkeypatch.setattr(agent_module, "recommend_action", lambda game: ActionSuggestion(
        {"command": "play", "card": 0, "target": 0}, "翻译后的说明，不含斩杀关键词", False,
        {"lethal_confirmed": True, "decision_basis": "verified_lethal"}))
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
    assert agent.choose_action(state) == {"command": "play", "card": 0, "target": 0}


def test_advice_payload_keeps_confidence_layers_separate():
    from spirebrain.driver.witness import Advice
    from spirebrain.overlay.feed import advice_event

    advice = Advice(
        point="combat", screen="COMBAT", command={"command": "end"}, key=("end",),
        label="结束回合", confidence=0.0, raw_model_confidence=None,
        local_confidence=0.72, selection_basis="jev_tactical_within_strategy",
        combat_facts={"lethal_confirmed": False},
    )
    payload = advice_event(advice)
    assert payload["confidence"] == 0.0
    assert payload["raw_model_confidence"] is None
    assert payload["local_confidence"] == 0.72
    assert payload["selection_basis"] == "jev_tactical_within_strategy"
    assert payload["combat_facts"]["lethal_confirmed"] is False


def test_trace_keeps_new_decision_evidence_fields_and_redacts_credentials(tmp_path):
    from spirebrain.driver.trace import DecisionTrace
    from spirebrain.redaction import redact_text

    trace = DecisionTrace(tmp_path / "trace.jsonl")
    assert trace.record("advice", {
        "run_id": "run", "run_epoch": 3, "state_id": "state",
        "decision_id": "decision", "advice_revision": 2,
        "raw_model_confidence": None, "local_confidence": 0.7,
        "selection_basis": "verified_lethal",
        "combat_facts": {"lethal_confirmed": True},
        "reason": "Bearer secret-token should be hidden",
    })
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8"))
    assert record["run_epoch"] == 3
    assert record["advice_revision"] == 2
    assert record["combat_facts"]["lethal_confirmed"] is True
    assert "secret-token" not in record["reason"]
    assert "[redacted]" in redact_text("Incorrect API key provided: secret-token")


def test_strategic_http_error_does_not_expose_key():
    from spirebrain.brain.gpt_client import OpenAIStrategicClient

    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "unauthorized", {},
            io.BytesIO(b'{"message":"Incorrect API key provided: secret-token"}'),
        )

    result = OpenAIStrategicClient(api_key="not-for-log", opener=opener).plan(
        {"state_id": "s", "run_id": "r"})
    assert result.fallback
    assert "secret-token" not in result.error
    assert result.error == "HTTP 401 from strategic provider"


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
