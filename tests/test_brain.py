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
from spirebrain.brain.orchestrator import StrategicOrchestrator
from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.legality import check_action
from spirebrain.jev_brain.client import JevAnswer, JevResponse


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
        def read(self, size=-1):
            data = json.dumps(body).encode("utf-8")
            return data if size < 0 else data[:size]

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


def test_responses_endpoint_uses_responses_wire_shape_and_parses_plan():
    plan_data = {
        "plan_id": "responses-plan", "state_id": "s-responses", "run_id": "r-responses",
        "current_objective": "按当前局面规划", "long_term_goal": "稳健通关",
        "priority": ["survive"], "preferred_candidates": [],
        "avoid_candidates": [], "resource_constraints": {},
        "next_steps": ["重新评估状态"], "replan_triggers": ["screen_change"],
        "reason": "固定协议测试", "uncertainty": "", "expires_after": 2,
    }
    response_body = {"model": "gpt-test", "output_text": json.dumps(plan_data)}
    requests = []

    class Response:
        def read(self, size=-1):
            data = json.dumps(response_body).encode("utf-8")
            return data if size < 0 else data[:size]
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def opener(request, timeout):
        requests.append(request)
        sent = json.loads(request.data.decode("utf-8"))
        assert request.full_url.endswith("/v1/responses")
        assert sent["input"]
        assert sent["instructions"]
        assert sent["max_output_tokens"] == 900
        assert "temperature" not in sent
        assert "messages" not in sent and "max_tokens" not in sent
        return Response()

    client = OpenAIStrategicClient(
        api_key="test", model="gpt-5.6-sol",
        endpoint="https://api.openai.com/v1/responses", opener=opener,
    )
    result = client.plan({"state_id": "s-responses", "run_id": "r-responses"})
    assert result.plan is not None
    assert result.plan.plan_id == "responses-plan"
    assert requests


def test_chat_completions_endpoint_keeps_compatible_wire_shape():
    client = OpenAIStrategicClient(
        api_key="test", model="kimi-k3",
        endpoint="https://kimi.test/v1/chat/completions",
    )
    body = client._request_body({"state_id": "s", "run_id": "r"})
    assert body["messages"][0]["role"] == "system"
    assert body["temperature"] == 1
    assert body["max_tokens"] == 900
    assert "input" not in body and "instructions" not in body


def test_gpt6sol_max_effort_uses_responses_reasoning_field():
    client = OpenAIStrategicClient(
        api_key="test", model="gpt-6-sol", reasoning_effort="max",
        endpoint="https://api.openai.com/v1/responses",
    )
    body = client._request_body({"state_id": "s", "run_id": "r"})
    assert body["model"] == "gpt-6-sol"
    assert body["reasoning"] == {"effort": "max"}
    assert "temperature" not in body


def test_hiyo_codex_preset_request_uses_responses_protocol_without_internal_timing():
    client = OpenAIStrategicClient(
        api_key="test", model="gpt-6-sol", reasoning_effort="max",
        endpoint="https://codex.hiyo.top/v1/responses",
    )
    body = client._request_body({
        "state_id": "state-1", "run_id": "run-1",
        "_internal_timing": {"queued_monotonic": 10.0, "queue_wait_ms": 12},
    })
    assert body["model"] == "gpt-6-sol"
    assert body["reasoning"] == {"effort": "max"}
    assert "temperature" not in body
    assert "messages" not in body and "input" in body
    assert "_internal_timing" not in body["input"]


def test_hiyo_responses_auth_failure_is_fast_and_does_not_echo_credentials():
    import io
    import urllib.error

    observed = []

    def opener(request, timeout):
        observed.append((request.full_url, timeout, request.get_header("Authorization")))
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {}, io.BytesIO(b"private response text"))

    result = OpenAIStrategicClient(
        api_key="fake-secret", model="gpt-6-sol", reasoning_effort="max",
        endpoint="https://codex.hiyo.top/v1/responses", timeout_ms=1250,
        opener=opener,
    ).plan({"state_id": "s", "run_id": "r"})

    assert result.fallback and result.error_kind == "auth"
    assert result.http_latency_ms is not None
    assert "fake-secret" not in result.error and "private response text" not in result.error
    assert observed == [(
        "https://codex.hiyo.top/v1/responses", 1.25, "Bearer fake-secret")]


def test_hiyo_timeout_is_classified_and_measured_without_retry():
    calls = []

    def opener(request, timeout):
        calls.append(timeout)
        raise TimeoutError("private timeout detail")

    result = OpenAIStrategicClient(
        api_key="fake-secret", model="gpt-6-sol",
        endpoint="https://codex.hiyo.top/v1/responses", timeout_ms=900,
        opener=opener,
    ).plan({"state_id": "s", "run_id": "r"})

    assert result.fallback and result.error_kind == "timeout"
    assert result.http_latency_ms is not None
    assert "private timeout detail" not in result.error
    assert calls == [0.9]


def test_strategic_provider_bounds_response_bytes_and_separates_http_parse_time():
    from spirebrain.brain.gpt_client import MAX_STRATEGIC_RESPONSE_BYTES

    payload = {
        "plan_id": "bounded-plan", "state_id": "bounded-state", "run_id": "bounded-run",
        "current_objective": "保持生存", "long_term_goal": "稳健推进",
        "priority": ["survive"], "preferred_candidates": [], "avoid_candidates": [],
        "resource_constraints": {}, "next_steps": [], "replan_triggers": ["screen_change"],
        "reason": "bounded response", "uncertainty": "", "expires_after": 1,
    }
    valid_bytes = json.dumps({"model": "gpt-6-sol", "output_text": json.dumps(payload)}).encode()

    class Response:
        def __init__(self, value): self.value = value
        def read(self, size=-1): return self.value if size < 0 else self.value[:size]
        def __enter__(self): return self
        def __exit__(self, *args): return False

    client = OpenAIStrategicClient(
        api_key="test", model="gpt-6-sol", reasoning_effort="max",
        endpoint="https://codex.hiyo.top/v1/responses",
        opener=lambda request, timeout: Response(valid_bytes),
    )
    result = client.plan({"state_id": "bounded-state", "run_id": "bounded-run"})
    assert result.plan is not None
    assert result.http_latency_ms is not None
    assert result.parse_latency_ms is not None
    assert result.response_bytes == len(valid_bytes)

    oversized = OpenAIStrategicClient(
        api_key="test", model="gpt-6-sol", endpoint="https://codex.hiyo.top/v1/responses",
        opener=lambda request, timeout: Response(b"x" * (MAX_STRATEGIC_RESPONSE_BYTES + 1)),
    ).plan({"state_id": "s", "run_id": "r"})
    assert oversized.fallback is True
    assert oversized.error_kind == "response_too_large"
    assert oversized.response_bytes == MAX_STRATEGIC_RESPONSE_BYTES + 1


def test_provider_trace_records_only_elapsed_stage_durations(tmp_path):
    from spirebrain.brain.gpt_client import LoggingStrategicClient

    events = []
    client = LoggingStrategicClient(
        MockStrategicClient(), tmp_path / "brain.jsonl",
        event_sink=lambda kind, fields: events.append((kind, fields)),
    )
    client.plan({
        "run_id": "run-1", "run_epoch": 1, "decision_id": "decision-1",
        "state_id": "state-1", "trigger": "combat_start",
        "strategy_fingerprint": "fingerprint-1",
        "strategic_scope_id": "scope-1",
        "_internal_timing": {
            "queued_monotonic": 10.0,
            "worker_started_monotonic": 10.025,
            "queue_wait_ms": 25,
        },
    })
    assert [kind for kind, _ in events] == ["provider_request", "provider_response"]
    for _, fields in events:
        assert fields["queue_wait_ms"] == 25
        assert fields["strategy_fingerprint"] == "fingerprint-1"
        assert fields["strategic_scope_id"] == "scope-1"
        assert "queued_monotonic" not in fields
        assert "worker_started_monotonic" not in fields


def test_configured_reasoning_effort_uses_chat_compatibility_field():
    client = OpenAIStrategicClient(
        api_key="test", model="gpt-6-sol", reasoning_effort="max",
        endpoint="https://gateway.test/v1/chat/completions",
    )
    body = client._request_body({"state_id": "s", "run_id": "r"})
    assert body["reasoning_effort"] == "max"
    assert "temperature" not in body


def test_responses_endpoint_can_request_json_schema_output():
    client = OpenAIStrategicClient(
        api_key="test", endpoint="https://api.openai.com/v1/responses",
        structured_output=True,
    )
    body = client._request_body({"state_id": "s", "run_id": "r"})
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["name"] == "strategic_plan"
    assert "response_format" not in body


def test_kimi_k3_uses_provider_supported_temperature():
    client = OpenAIStrategicClient(api_key="test", model="kimi-k3")
    body = client._request_body({"state_id": "s1", "run_id": "r1"})
    assert body["temperature"] == 1


def test_other_models_keep_low_temperature_planning_default():
    client = OpenAIStrategicClient(api_key="test", model="gpt-test")
    body = client._request_body({"state_id": "s1", "run_id": "r1"})
    assert body["temperature"] == 0.1


def test_deepseek_compatible_model_requests_portable_json_object():
    client = OpenAIStrategicClient(
        api_key="test", model="deepseek-flash",
        endpoint="https://api.deepseek.com/v1/chat/completions",
    )
    body = client._request_body({"state_id": "s1", "run_id": "r1"})
    assert body["response_format"] == {"type": "json_object"}
    from spirebrain.brain.gpt_client import PLAN_SCHEMA
    schema = json.loads(body["messages"][0]["content"].split("JSON Schema:\n", 1)[1])
    assert schema == PLAN_SCHEMA


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


def test_strategic_context_is_visible_when_local_executor_picks_legal_move(tmp_path):
    class ContextOnlyClient:
        backend_name = "mock"
        async_required = False

        def plan(self, payload):
            state_id = str(payload["state_id"])
            run_id = str(payload["run_id"])
            plan = StrategicPlan.from_dict({
                "plan_id": "context-only", "state_id": state_id,
                "run_id": run_id, "current_objective": "优先保留生命和能量",
                "long_term_goal": "通关", "priority": ["survive"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "战略上下文", "uncertainty": "",
                "expires_after": 2,
            }, state_id=state_id, run_id=run_id)
            return BrainResponse(plan=plan, backend="mock")

    client = ContextOnlyClient()
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=client, log_dir=tmp_path)
    command = agent.choose_action(_combat())
    assert check_action(_combat(), command)[0]
    detail = agent.history[-1]["detail"]
    assert detail["brain_source"] == "gpt_strategy"
    assert detail["strategic_goal"] == "优先保留生命和能量"
    assert detail["selection_basis"] == "strategic_context_local_choice"
    assert detail["source_type"] == "rule_fallback"
    assert detail["reason"] != "战略上下文"


def test_stale_strategic_goal_guides_jev_without_reusing_candidate_ids(tmp_path):
    goal = "为精英战保留能量"
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=MockStrategicClient(), log_dir=tmp_path)
    game = _combat(run_id="run-context")
    agent.observe(game)
    agent.strategic._retain_context_plan(StrategicPlan(
        plan_id="late-context", state_id="old-state",
        run_id=agent.strategic.memory.run_id,
        current_objective=goal,
        preferred_candidates=["combat:play:0:0"],
        avoid_candidates=["combat:end"],
    ), game, "superseded_request")
    agent._active_candidates = build_action_candidates(game)
    agent._active_plan = None

    class CapturingJev:
        request_state = None
        criteria = None

        def ask(self, state, questions):
            self.request_state = state
            self.criteria = questions["tactical"].criteria
            candidate_id = next(iter(self.criteria))
            return JevResponse(answers={
                "tactical": JevAnswer(value=candidate_id, confidence=0.9)
            })

    jev = CapturingJev()
    agent.jev = jev
    picked = agent._jev_tactical_pick(game)

    assert picked is not None
    candidate, _, guidance = picked
    assert jev.request_state["tactical_goal"] == goal
    assert guidance == {
        "plan_id": "late-context",
        "goal": goal,
        "resource_constraints": {},
        "context_only": True,
    }
    assert candidate.candidate_id in jev.criteria
    assert check_action(game, candidate.command)[0]
    # The stale positional preference and avoidance are not model input and
    # cannot filter or identify a current candidate.
    assert "preferred_candidates" not in jev.request_state
    assert "avoid_candidates" not in jev.request_state


def test_retained_strategic_context_is_visible_on_current_legal_choice(tmp_path):
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=MockStrategicClient(), log_dir=tmp_path)
    game = _combat(run_id="run-context")
    agent.observe(game)
    stale = StrategicPlan(
        plan_id="late-context", state_id="old-state",
        run_id=agent.strategic.memory.run_id,
        current_objective="为精英战保留能量",
        preferred_candidates=["combat:play:0:0"],
        avoid_candidates=["combat:end"],
    )
    agent.strategic._retain_context_plan(stale, game, "superseded_request")
    candidates = build_action_candidates(game)
    command, decision = agent.strategic.choose(
        game, {"command": "end"}, candidates=candidates,
    )

    assert check_action(game, command)[0]
    assert decision.source_type == "gpt_context"
    assert decision.selection_basis == "strategic_context_local_choice"
    assert decision.strategic_goal == "为精英战保留能量"
    assert decision.plan_id == "late-context"
    # The stale preferred/avoid IDs were stripped before this choice; the
    # current legal fallback is never silently replaced by an old slot.
    assert command == {"command": "end"}


def test_shop_reuses_phase_strategy_after_purchase_and_replans_next_floor(tmp_path):
    client = MockStrategicClient()
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=client, log_dir=tmp_path)
    first = agent.choose_action(_shop(100))
    assert check_action(_shop(100), first)[0]
    calls_after_first = client.calls
    second_state = _shop(30)
    second = agent.choose_action(second_state)
    assert check_action(second_state, second)[0]
    assert client.calls == calls_after_first
    assert second == {"command": "return"}

    next_shop = _shop(75)
    next_shop["floor"] = 9
    next_command = agent.choose_action(next_shop)
    assert check_action(next_shop, next_command)[0]
    assert client.calls == calls_after_first + 1


def test_phase_strategy_goal_reaches_local_shop_judgment(monkeypatch, tmp_path):
    from spirebrain.driver import agent as agent_module
    from spirebrain.jev_brain.decisions import Decision

    seen_goals = []

    class CapturingShopDecider:
        def __init__(self, jev, *, goal, run):
            seen_goals.append(goal)

        def decide(self, **kwargs):
            return Decision("shop", "leave", 1.0, True, {})

    monkeypatch.setattr(agent_module, "ShopDecider", CapturingShopDecider)
    client = MockStrategicClient(objective="为精英战保留金币，优先提升力量")
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            brain_client=client, log_dir=tmp_path)
    agent.choose_action(_shop(100))

    after_purchase = _shop(50)
    after_purchase["screen_state"]["cards"][0] = {"id": "Limit Break", "price": 50}
    agent.choose_action(after_purchase)

    assert client.calls == 1
    assert len(seen_goals) == 2
    assert all("为精英战保留金币，优先提升力量" in goal for goal in seen_goals)


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


def test_strategic_gateway_never_builds_responses_payload():
    client = OpenAIStrategicClient(api_key="test", endpoint="https://gateway.test/v1")
    body = client._request_body({"state_id": "s", "run_id": "r"})
    assert "messages" in body
    assert "input" not in body
    assert "text" not in body


def test_kimi_style_unicode_json_response_is_accepted():
    import json
    base = {"plan_id": "p", "state_id": "s", "run_id": "r",
            "current_objective": "防御：中文", "long_term_goal": "生存",
            "priority": [], "preferred_candidates": [], "avoid_candidates": [],
            "resource_constraints": {}, "next_steps": [], "replan_triggers": [],
            "reason": "中文：理由", "uncertainty": "", "expires_after": 1}
    payload = {"choices": [{"message": {"content": json.dumps(base, ensure_ascii=False)}}]}

    class Response:
        def read(self, size=-1):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return data if size < 0 else data[:size]
        def __enter__(self): return self
        def __exit__(self, *args): return False

    result = OpenAIStrategicClient(
        api_key="test", model="kimi-k3", endpoint="https://kimi.test/v1/chat/completions",
        opener=lambda request, timeout: Response(),
    ).plan({"state_id": "s", "run_id": "r"})
    assert result.fallback is False
    assert result.plan.current_objective == "防御：中文"


@pytest.mark.parametrize("content", [
    [{"type": "text", "text": '{"plan_id":"p","state_id":"s","run_id":"r",'
      '"current_objective":"列表响应","long_term_goal":"生存",'
      '"priority":[],"preferred_candidates":[],"avoid_candidates":[],'
      '"resource_constraints":{},"next_steps":[],"replan_triggers":[],'
      '"reason":"ok","uncertainty":"","expires_after":1}'}],
    {"type": "text", "text": '{"plan_id":"p","state_id":"s","run_id":"r",'
      '"current_objective":"对象响应","long_term_goal":"生存",'
      '"priority":[],"preferred_candidates":[],"avoid_candidates":[],'
      '"resource_constraints":{},"next_steps":[],"replan_triggers":[],'
      '"reason":"ok","uncertainty":"","expires_after":1}'},
])
def test_openai_client_accepts_text_block_content_shapes(content):
    payload = {"choices": [{"message": {"content": content}}]}

    class Response:
        def read(self, size=-1):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return data if size < 0 else data[:size]
        def __enter__(self): return self
        def __exit__(self, *args): return False

    result = OpenAIStrategicClient(
        api_key="test", endpoint="https://example.test/v1/chat/completions",
        opener=lambda request, timeout: Response(),
    ).plan({"state_id": "s", "run_id": "r"})
    assert result.fallback is False
    assert result.plan.current_objective in {"列表响应", "对象响应"}


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
        def read(self, size=-1):
            data = json.dumps(body).encode()
            return data if size < 0 else data[:size]
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
        def read(self, size=-1):
            data = json.dumps(body).encode()
            return data if size < 0 else data[:size]
        def __enter__(self): return self
        def __exit__(self, *args): return False

    client = OpenAIStrategicClient(
        api_key="test", endpoint="https://example.test/v1/chat/completions",
        opener=lambda request, timeout: Response(),
    )
    result = client.plan({"state_id": "s", "run_id": "r", "max_plan_steps": 2})
    assert result.plan is not None
    assert result.plan.preferred_candidates == ["combat:end"]


@pytest.mark.parametrize("message", [
    {"content": "<think>{not the plan}</think>\n```json\n"
     '{"plan_id":"p","state_id":"s","run_id":"r",'
     '"current_objective":"思考标签","long_term_goal":"生存",'
     '"priority":[],"preferred_candidates":[],"avoid_candidates":[],'
     '"resource_constraints":{},"next_steps":[],"replan_triggers":[],'
     '"reason":"ok","uncertainty":"","expires_after":1}\n```'},
    {"content": None, "tool_calls": [{"function": {"arguments":
     '{"plan_id":"p","state_id":"s","run_id":"r",'
     '"current_objective":"工具参数","long_term_goal":"生存",'
     '"priority":[],"preferred_candidates":[],"avoid_candidates":[],'
     '"resource_constraints":{},"next_steps":[],"replan_triggers":[],'
     '"reason":"ok","uncertainty":"","expires_after":1}'}}]},
    {"content": None, "reasoning_content":
     '{"plan_id":"p","state_id":"s","run_id":"r",'
     '"current_objective":"空内容备用通道","long_term_goal":"生存",'
     '"priority":[],"preferred_candidates":[],"avoid_candidates":[],'
     '"resource_constraints":{},"next_steps":[],"replan_triggers":[],'
     '"reason":"ok","uncertainty":"","expires_after":1}'},
])
def test_openai_client_recovers_common_gateway_plan_channels(message):
    payload = {"choices": [{"message": message}]}

    class Response:
        def read(self, size=-1):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return data if size < 0 else data[:size]
        def __enter__(self): return self
        def __exit__(self, *args): return False

    result = OpenAIStrategicClient(
        api_key="test", endpoint="https://example.test/v1/chat/completions",
        opener=lambda request, timeout: Response(),
    ).plan({"state_id": "s", "run_id": "r"})
    assert result.fallback is False
    assert result.plan is not None
    assert result.plan.state_id == "s"


def test_openai_client_reports_empty_gateway_response_as_parse_failure():
    payload = {"choices": [{"message": {"content": None}}]}

    class Response:
        def read(self, size=-1):
            data = json.dumps(payload).encode("utf-8")
            return data if size < 0 else data[:size]
        def __enter__(self): return self
        def __exit__(self, *args): return False

    result = OpenAIStrategicClient(
        api_key="test", endpoint="https://example.test/v1/chat/completions",
        opener=lambda request, timeout: Response(),
    ).plan({"state_id": "s", "run_id": "r"})
    assert result.fallback is True
    assert result.error_kind == "parse_or_validation"
    assert "Authorization" not in result.error


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
    assert result.error_kind == "plan_validation"


def test_async_plan_validation_rejection_is_correlated_in_trace():
    called = threading.Event()

    class InvalidProvider:
        backend_name = "scripted"
        async_required = True

        def plan(self, payload):
            called.set()
            plan = StrategicPlan.from_dict({
                "plan_id": "bad-plan", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "x",
                "long_term_goal": "x", "priority": [],
                "preferred_candidates": ["not-in-state"],
                "avoid_candidates": [], "resource_constraints": {},
                "next_steps": [], "replan_triggers": [], "reason": "x",
                "uncertainty": "", "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend=self.backend_name,
                                 request_id="request-invalid-plan")

    events = []
    orchestrator = StrategicOrchestrator(
        client=InvalidProvider(), backend="scripted", async_planning=True)
    orchestrator.event_sink = lambda event_type, fields: events.append(
        {"event_type": event_type, **fields})
    try:
        state = _combat()
        orchestrator.plan_for(state, decision_id="decision-invalid-plan")
        assert called.wait(1)
        thread = orchestrator._plan_thread
        if thread is not None:
            thread.join(1)

        rejected = next(event for event in events
                        if event["event_type"] == "plan_validation_rejected")
        assert rejected["decision_id"] == "decision-invalid-plan"
        from spirebrain.brain.planner import stable_state_id
        assert rejected["state_id"] == stable_state_id(state)
        assert rejected["request_id"] == "request-invalid-plan"
        assert rejected["error_kind"] == "plan_validation"
        from spirebrain.analysis.event_contract import make_event, validate_event
        envelope = make_event(rejected.pop("event_type"), fields=rejected)
        assert validate_event(envelope, strict=True) == []
        from spirebrain.analysis.event_contract import decision_chain
        chain = decision_chain([envelope], "decision-invalid-plan")
        assert len(chain["plan_validations"]) == 1
        assert chain["request_lifecycle"]["response_count"] == 0
    finally:
        orchestrator.close()


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


def test_provider_trace_context_carries_decision_generation_and_trigger(tmp_path):
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            async_planning=False, log_dir=tmp_path)
    agent.choose_action(_combat())

    context = agent._provider_trace_context()

    assert context["generation"] == agent._decision_sequence
    assert context["trigger"] == "combat_start"


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
    # The response was issued for the original combat snapshot.  It is still
    # useful strategic context after harmless combat churn, while the plan is
    # rebound to the current state and concrete candidates are revalidated.
    assert len(provider.calls) == 1


def test_strategic_plan_is_not_recalled_after_tactical_use_count_expires():
    class Provider:
        backend_name = "phase-test"
        async_required = False

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            plan = StrategicPlan.from_dict({
                "plan_id": "combat-phase", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "稳住当前战斗",
                "long_term_goal": "保持构筑资源完整", "priority": ["survive"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": ["评估敌方意图"],
                "replan_triggers": ["enemy_changed"], "reason": "阶段策略",
                "uncertainty": "", "expires_after": 1,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend=self.backend_name)

    provider = Provider()
    orchestrator = StrategicOrchestrator(
        client=provider, backend=provider.backend_name, async_planning=False)
    state = _combat()
    plan, _ = orchestrator.plan_for(state)
    assert plan is not None

    for index in range(6):
        changed = json.loads(json.dumps(state))
        changed["combat"]["player"]["energy"] = index % 3
        changed["combat"]["hand"][0]["id"] = "Strike_R" if index % 2 else "Defend_R"
        candidates = build_action_candidates(changed)
        command, _ = orchestrator.choose(
            changed, {"command": "end"}, candidates=candidates, plan=plan)
        assert check_action(changed, command)[0]
        plan, _ = orchestrator.plan_for(changed, candidates=candidates)
        assert plan is not None

    assert provider.calls == 1


def test_enemy_group_change_is_a_strategic_checkpoint():
    class Provider:
        backend_name = "enemy-change-test"
        async_required = False

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            state_id = payload["state_id"]
            run_id = payload["run_id"]
            plan = StrategicPlan.from_dict({
                "plan_id": f"enemy-phase-{self.calls}", "state_id": state_id,
                "run_id": run_id, "current_objective": "评估敌方威胁",
                "long_term_goal": "保持生存", "priority": ["survive"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": ["enemy_changed"], "reason": "新遭遇",
                "uncertainty": "", "expires_after": 5,
            }, state_id=state_id, run_id=run_id)
            return BrainResponse(plan=plan, backend=self.backend_name)

    provider = Provider()
    orchestrator = StrategicOrchestrator(
        client=provider, backend=provider.backend_name, async_planning=False)
    state = _combat()
    orchestrator.plan_for(state)
    assert provider.calls == 1

    changed = json.loads(json.dumps(state))
    changed["combat"]["monsters"][0]["id"] = "JawWorm"
    changed["combat"]["monsters"][0]["name"] = "Jaw Worm"
    orchestrator.plan_for(changed)
    assert provider.calls == 2


def test_same_size_deck_change_is_a_strategic_checkpoint():
    client = MockStrategicClient()
    orchestrator = StrategicOrchestrator(
        client=client, backend="mock", async_planning=False)
    state = _combat(deck=["Strike_R", "Defend_R"])
    orchestrator.plan_for(state)
    assert client.calls == 1

    changed = json.loads(json.dumps(state))
    changed["deck"][0] = "Bash"
    orchestrator.plan_for(changed)
    assert client.calls == 2


def test_failed_strategic_request_is_cooled_down_until_phase_changes():
    now = [100.0]

    class Provider:
        backend_name = "offline-test"
        async_required = False

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            return BrainResponse(backend=self.backend_name, error="timeout",
                                 error_kind="timeout", fallback=True)

    provider = Provider()
    orchestrator = StrategicOrchestrator(
        client=provider, backend=provider.backend_name, async_planning=False,
        plan_retry_cooldown_ms=30_000, clock=lambda: now[0])
    state = _combat()
    orchestrator.plan_for(state, trigger="combat_start")
    assert provider.calls == 1

    same_encounter = json.loads(json.dumps(state))
    same_encounter["combat"]["player"]["energy"] = 0
    orchestrator.plan_for(same_encounter, trigger="combat_start")
    assert provider.calls == 1
    assert orchestrator.plan_attempts_suppressed == 1

    new_encounter = json.loads(json.dumps(same_encounter))
    new_encounter["combat"]["monsters"][0]["id"] = "JawWorm"
    new_encounter["combat"]["monsters"][0]["name"] = "Jaw Worm"
    orchestrator.plan_for(new_encounter)
    assert provider.calls == 2


def test_repeated_combat_start_does_not_supersede_pending_same_encounter_plan():
    started = threading.Event()
    release = threading.Event()

    class SlowProvider:
        backend_name = "slow-phase-test"
        async_required = True

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            started.set()
            assert release.wait(2)
            plan = StrategicPlan.from_dict({
                "plan_id": "combat-phase", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "处理当前威胁",
                "long_term_goal": "保留生命", "priority": ["survive"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": ["enemy_changed"], "reason": "慢响应",
                "uncertainty": "", "expires_after": 2,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend=self.backend_name)

    provider = SlowProvider()
    orchestrator = StrategicOrchestrator(
        client=provider, backend=provider.backend_name, async_planning=True)
    state = _combat()
    orchestrator.plan_for(state, trigger="combat_start")
    assert started.wait(1)

    changed = json.loads(json.dumps(state))
    changed["combat"]["player"]["energy"] = 0
    orchestrator.plan_for(changed, trigger="combat_start")
    assert provider.calls == 1
    assert orchestrator.invalidated_plan_requests == 0

    release.set()
    deadline = time.monotonic() + 2
    accepted = None
    while time.monotonic() < deadline:
        accepted, _ = orchestrator.plan_for(changed, trigger="combat_start")
        if accepted is not None:
            break
        time.sleep(0.01)
    assert accepted is not None
    assert provider.calls == 1
    assert orchestrator.invalidated_plan_requests == 0
    orchestrator.close()


def test_shop_phase_reuses_strategy_without_reusing_changed_item_identity():
    client = MockStrategicClient(preferred=["shop:item:0"], objective="优先购买力量成长")
    orchestrator = StrategicOrchestrator(
        client=client, backend="mock", async_planning=False)
    initial = _shop(100)
    initial_candidates = build_action_candidates(initial)
    plan, _ = orchestrator.plan_for(initial, candidates=initial_candidates)
    assert plan is not None

    first_command, first_decision = orchestrator.choose(
        initial, {"command": "return"}, candidates=initial_candidates, plan=plan)
    assert first_command == {"command": "choose", "choice": 0}
    assert first_decision.source_type == "gpt_strategy"

    changed = json.loads(json.dumps(initial))
    changed["gold"] = 50
    changed["screen_state"]["cards"][0] = {"id": "Limit Break", "price": 50}
    changed_candidates = build_action_candidates(changed)
    refreshed_plan, _ = orchestrator.plan_for(changed, candidates=changed_candidates)
    assert client.calls == 1

    fallback = {"command": "choose", "choice": 2}
    command, decision = orchestrator.choose(
        changed, fallback, candidates=changed_candidates, plan=refreshed_plan)
    assert command == fallback
    assert decision.primary_candidate.candidate_id == "shop:item:2"
    assert decision.source_type != "gpt_strategy"
    assert check_action(changed, command)[0]

    later_shop = _shop(75)
    later_shop["floor"] = 9
    orchestrator.plan_for(later_shop, candidates=build_action_candidates(later_shop))
    assert client.calls == 2
