"""Strategic brain, candidate broker and GPT fallback tests."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from spirebrain.brain.action_broker import build_action_candidates, potion_purchase_allowed, reconcile
from spirebrain.brain.gpt_client import MockStrategicClient, OpenAIStrategicClient
from spirebrain.brain.memory import RunMemory
from spirebrain.brain.planner import StrategicPlanner
from spirebrain.brain.protocol import BrainResponse, PlanValidationError, StrategicPlan
from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.legality import check_action


def _combat(**changes):
    state = {
        "character": "IRONCLAD",
        "act": 1,
        "floor": 3,
        "current_hp": 50,
        "max_hp": 80,
        "gold": 100,
        "screen_type": "COMBAT",
        "available_commands": ["play", "potion", "end", "wait", "state"],
        "combat": {
            "player": {"energy": 1, "current_hp": 50, "block": 0},
            "hand": [{"id": "Strike_R", "type": "ATTACK", "cost": 1,
                      "damage": 6, "has_target": True}],
            "monsters": [{"id": "Cultist", "current_hp": 20,
                          "intent": "ATTACK", "damage": 8}],
        },
    }
    state.update(changes)
    return state


def _shop(gold=100):
    return {
        "character": "IRONCLAD", "act": 1, "floor": 5,
        "current_hp": 60, "max_hp": 80, "gold": gold,
        "screen_type": "SHOP", "available_commands": ["choose", "return", "wait", "state"],
        "screen_state": {
            "cards": [{"id": "Inflame", "price": 50}, {"id": "Demon Form", "price": 120}],
            "relics": [{"id": "Anchor", "price": 40}],
            "potions": [{"id": "Fire Potion", "price": 30}],
            "purge_cost": 75,
        },
    }


def test_plan_rejects_a_different_state():
    with pytest.raises(PlanValidationError):
        from spirebrain.brain.protocol import StrategicPlan
        StrategicPlan.from_dict({"plan_id": "x", "state_id": "old"},
                                state_id="new", run_id="local")


def test_mock_plan_selects_only_a_legal_candidate():
    state = _combat()
    candidates = build_action_candidates(state)
    memory = RunMemory()
    memory.observe(state, state_id="state")
    response = StrategicPlanner(MockStrategicClient()).plan(
        state, candidates, memory=memory)
    command, decision = reconcile(
        game=state, candidates=candidates, fallback={"command": "end"},
        plan=response.plan, state_id="later",
    )
    assert check_action(state, command)[0]
    assert decision.primary_candidate.candidate_id == "combat:play:0:0"
    assert decision.alternative_candidate is not None


def test_alternative_candidate_obeys_same_resource_constraints():
    state = _combat()
    candidates = build_action_candidates(state)
    plan = StrategicPlan("p", "s", "local", current_objective="survive",
                         resource_constraints={"max_cost": 0})
    command, decision = reconcile(game=state, candidates=candidates,
                                  fallback={"command": "end"}, plan=plan,
                                  state_id="s")
    assert decision.alternative_candidate is None


def test_memory_resets_on_game_over():
    memory = RunMemory()
    memory.observe(_combat(), state_id="one")
    memory.record_action({"command": "end"})
    memory.observe({"run_id": "new", "screen_type": "DEATH", "character": "IRONCLAD"})
    assert not memory.events
    assert memory.plan_id == ""


def test_openai_response_is_parsed_without_exposing_commands():
    body = {
        "model": "gpt-test",
        "output_text": json.dumps({
            "plan_id": "p1", "state_id": "s1", "run_id": "r1",
            "current_objective": "先活下来", "long_term_goal": "通关",
            "priority": ["survive"], "preferred_candidates": ["combat:end"],
            "avoid_candidates": [], "resource_constraints": {},
            "next_steps": ["结束回合"], "replan_triggers": ["hp_change"],
            "reason": "当前没有必要冒险", "uncertainty": "", "expires_after": 1,
        }, ensure_ascii=False),
    }

    class Response:
        def read(self):
            return json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response()

    client = OpenAIStrategicClient(api_key="test", model="gpt-test",
                                   opener=opener)
    result = client.plan({"state_id": "s1", "run_id": "r1", "candidates": []})
    assert result.plan.current_objective == "先活下来"
    assert "command" not in result.plan.raw
    assert requests and "Authorization" in requests[0][0].headers


def test_agent_uses_gpt_preference_and_keeps_command_legal(tmp_path):
    client = MockStrategicClient(preferred=["combat:end"], objective="保留能量")
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=client, log_dir=tmp_path)
    command = agent.choose_action(_combat())
    assert command == {"command": "end"}
    assert check_action(_combat(), command)[0]
    detail = agent.history[-1]["detail"]
    assert detail["source_type"] == "gpt_strategy"
    assert detail["strategic_goal"] == "保留能量"
    assert detail["alternative_command"] == {"command": "play", "card": 0, "target": 0}


def test_shop_filters_unaffordable_items_and_replans_after_gold_change(tmp_path):
    client = MockStrategicClient()
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=client, log_dir=tmp_path)
    first = agent.choose_action(_shop(100))
    assert check_action(_shop(100), first)[0]
    calls_after_first = client.calls
    second_state = _shop(30)
    second = agent.choose_action(second_state)
    assert check_action(second_state, second)[0]
    assert client.calls > calls_after_first
    assert second.get("choice") in {3, 4}


def test_openai_without_key_is_a_nonfatal_fallback(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = OpenAIStrategicClient(api_key="").plan({"state_id": "s", "run_id": "r"})
    assert result.plan is None
    assert result.fallback
    assert "OPENAI_API_KEY" in result.error


def test_openai_compatible_endpoint_uses_chat_completions_payload():
    client = OpenAIStrategicClient(
        api_key="test", model="gpt-5.6-sol",
        endpoint="https://codex.example/v1/chat/completions",
    )
    body = client._request_body({"state_id": "s", "run_id": "r"})
    assert body["model"] == "gpt-5.6-sol"
    assert body["messages"][0]["role"] == "system"
    assert "response_format" not in body
    assert "input" not in body


def test_openai_compatible_endpoint_can_opt_into_structured_output():
    client = OpenAIStrategicClient(
        api_key="test", model="gpt-test",
        endpoint="https://codex.example/v1/chat/completions",
        structured_output=True,
    )
    body = client._request_body({"state_id": "s", "run_id": "r"})
    assert body["response_format"]["type"] == "json_schema"


def test_strategic_backend_accepts_mainland_compatible_provider():
    from spirebrain.brain.gpt_client import get_strategic_brain

    client = get_strategic_brain(
        "deepseek", model="deepseek-chat",
        endpoint="https://api.deepseek.com/v1/chat/completions",
        api_key="test",
    )
    assert client.backend_name == "deepseek"
    assert client.endpoint.endswith("/v1/chat/completions")


def test_openai_client_accepts_common_plan_wrapper():
    import json

    base = {
        "plan_id": "p", "state_id": "s", "run_id": "r",
        "current_objective": "survive", "long_term_goal": "win",
        "priority": ["survive"], "preferred_candidates": [],
        "avoid_candidates": [], "resource_constraints": {},
        "next_steps": ["wait"], "replan_triggers": [], "reason": "ok",
        "uncertainty": "", "expires_after": 1,
    }
    body = {"choices": [{"message": {"content": json.dumps({"plan": base})}}]}

    class Response:
        def read(self): return json.dumps(body).encode()
        def __enter__(self): return self
        def __exit__(self, *args): return False

    client = OpenAIStrategicClient(
        api_key="test", endpoint="https://example.test/v1/chat/completions",
        opener=lambda request, timeout: Response(),
    )
    result = client.plan({"state_id": "s", "run_id": "r"})
    assert result.plan is not None
    assert not result.fallback


def test_openai_client_normalizes_compact_steps_response():
    import json

    body = {"choices": [{"message": {"content": json.dumps({
        "steps": [{"candidate_id": "combat:end", "reason": "先保留能量"}],
    })}}]}

    class Response:
        def read(self): return json.dumps(body).encode()
        def __enter__(self): return self
        def __exit__(self, *args): return False

    client = OpenAIStrategicClient(
        api_key="test", endpoint="https://example.test/v1/chat/completions",
        opener=lambda request, timeout: Response(),
    )
    result = client.plan({"state_id": "s", "run_id": "r", "max_plan_steps": 2})
    assert result.plan is not None
    assert result.plan.preferred_candidates == ["combat:end"]


def test_planner_rejects_unknown_candidate_reference():
    class InvalidProvider:
        backend_name = "mock"

        def plan(self, payload):
            plan = StrategicPlan.from_dict({
                "plan_id": "bad", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "x",
                "long_term_goal": "x", "priority": [],
                "preferred_candidates": ["not-in-state"],
                "avoid_candidates": [], "resource_constraints": {},
                "next_steps": [], "replan_triggers": [], "reason": "x",
                "uncertainty": "", "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend="mock")

    state = _combat()
    memory = RunMemory()
    memory.observe(state)
    result = StrategicPlanner(InvalidProvider()).plan(
        state, build_action_candidates(state), memory=memory)
    assert result.plan is None and result.fallback
    assert "不存在的候选" in result.error


def test_candidate_broker_applies_hard_forbidden_and_potion_capacity():
    state = {"screen_type": "MAP", "available_commands": ["choose"],
             "map": {"next_nodes": [{"symbol": "M"}, {"symbol": "R"}]}}
    assert [c.candidate_id for c in build_action_candidates(state, forbidden_indices={0})] == ["map:1"]
    shop = _shop(100)
    shop["potions"] = [{"id": "Potion", "can_use": True}, {"id": "Potion", "can_use": True}]
    shop["screen_state"]["potion_capacity"] = 2
    assert not potion_purchase_allowed(shop, {"potion_full": False}, shop["screen_state"])


def test_unknown_combat_values_degrade_to_a_legal_fallback(tmp_path):
    state = _combat()
    state["combat"]["hand"][0]["damage"] = None
    state["combat"]["player"]["energy"] = None
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock", log_dir=tmp_path)
    command = agent.choose_action(state)
    assert command.get("command") in {"play", "end", "state"}


def test_combat_reuses_strategy_between_ordinary_card_steps(tmp_path):
    client = MockStrategicClient()
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=client, log_dir=tmp_path)
    state = _combat()
    agent.choose_action(state)
    state["combat"]["hand"] = []
    state["combat"]["player"]["energy"] = 0
    agent.choose_action(state)
    assert client.calls == 1


def test_network_strategic_planning_is_non_blocking_and_state_bound():
    """The CommunicationMod-facing call must not wait for a slow provider."""
    started = threading.Event()
    release = threading.Event()

    class SlowProvider:
        backend_name = "openai"
        async_required = True

        def __init__(self):
            self.calls = []

        def plan(self, payload):
            self.calls.append(payload["state_id"])
            started.set()
            release.wait(2)
            state_id = payload["state_id"]
            run_id = payload["run_id"]
            plan = StrategicPlan.from_dict({
                "plan_id": "slow-" + state_id[:6], "state_id": state_id,
                "run_id": run_id, "current_objective": "保留生命",
                "long_term_goal": "通关", "priority": ["survive"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "慢 provider 测试",
                "uncertainty": "", "expires_after": 2,
            }, state_id=state_id, run_id=run_id)
            return BrainResponse(plan=plan, backend="openai")

    state = _combat()
    candidates = build_action_candidates(state)
    provider = SlowProvider()
    from spirebrain.brain.orchestrator import StrategicOrchestrator
    orchestrator = StrategicOrchestrator(client=provider, backend="openai")
    began = time.monotonic()
    plan, response = orchestrator.plan_for(state, candidates=candidates)
    assert plan is None
    assert time.monotonic() - began < 0.2
    assert started.wait(1)

    changed = _combat(current_hp=49)
    changed_candidates = build_action_candidates(changed)
    orchestrator.plan_for(changed, candidates=changed_candidates)
    release.set()
    deadline = time.monotonic() + 2
    accepted = None
    while time.monotonic() < deadline:
        accepted, _ = orchestrator.plan_for(changed, candidates=changed_candidates)
        if accepted is not None:
            break
        time.sleep(0.01)
    assert accepted is not None
    assert accepted.state_id == orchestrator.last_state_id
    assert provider.calls[-1] == orchestrator.last_state_id
