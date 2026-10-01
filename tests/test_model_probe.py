"""Dashboard provider checks must validate a parsed strategic plan offline."""

from __future__ import annotations

import json

import pytest

from spirebrain.brain.gpt_client import OpenAIStrategicClient
from spirebrain.overlay.config import test_brain_connection as probe_brain_connection


def _provider_factory(response_text: str, seen: list[dict]):
    class Response:
        def read(self, size=-1):
            data = json.dumps({"model": "fake-model", "output_text": response_text}).encode()
            return data if size < 0 else data[:size]
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def factory(**kwargs):
        def opener(request, timeout):
            seen.append({
                "url": request.full_url,
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
                "authorization": request.headers.get("Authorization"),
            })
            return Response()

        return OpenAIStrategicClient(**kwargs, opener=opener)
    return factory


@pytest.mark.parametrize("endpoint,protocol", [
    ("https://api.openai.com/v1/responses", "responses"),
    ("https://gateway.example/v1/chat/completions", "chat"),
])
def test_dashboard_probe_uses_runtime_protocol_and_validates_plan(endpoint, protocol):
    plan = {
        "plan_id": "probe-plan",
        "state_id": "dashboard-provider-probe-state",
        "run_id": "dashboard-provider-probe-run",
        "current_objective": "评估当前阶段",
        "long_term_goal": "稳健推进",
        "priority": ["survive"],
        "preferred_candidates": ["probe:wait"],
        "avoid_candidates": [],
        "resource_constraints": {},
        "next_steps": ["根据下一状态再评估"],
        "replan_triggers": ["screen_change"],
        "reason": "离线协议测试",
        "uncertainty": "",
        "expires_after": 2,
    }
    seen = []
    result = probe_brain_connection(
        {"openai_model": "fake-model", "openai_endpoint": endpoint},
        "probe-secret",
        provider_factory=_provider_factory(json.dumps(plan, ensure_ascii=False), seen),
    )

    assert result["ok"] is True
    assert "可解析的战略计划" in result["message"]
    assert result["model"] == "fake-model"
    assert len(seen) == 1
    assert seen[0]["authorization"] == "Bearer probe-secret"
    assert "probe-secret" not in json.dumps(result)
    if protocol == "responses":
        assert "input" in seen[0]["body"]
        assert "messages" not in seen[0]["body"]
    else:
        assert "messages" in seen[0]["body"]
        assert "input" not in seen[0]["body"]


def test_dashboard_probe_rejects_http_200_without_valid_plan():
    seen = []
    result = probe_brain_connection(
        {"openai_model": "fake-model",
         "openai_endpoint": "https://api.openai.com/v1/responses"},
        "probe-secret",
        provider_factory=_provider_factory("OK", seen),
    )

    assert result["ok"] is False
    assert result["error_kind"] == "parse_or_validation"
    assert "有效战略计划" in result["message"]
    assert "probe-secret" not in json.dumps(result)


def test_dashboard_probe_reports_missing_key_without_provider_request():
    called = []
    result = probe_brain_connection(
        {"openai_model": "fake-model",
         "openai_endpoint": "https://api.openai.com/v1/responses"},
        "",
        provider_factory=lambda **kwargs: called.append(kwargs),
    )

    assert result["ok"] is False
    assert result["message"] == "尚未配置 GPT API Key"
    assert called == []


def test_dashboard_probe_surfaces_safe_http_diagnostics_without_body():
    from spirebrain.brain.protocol import BrainResponse

    def provider_factory(**kwargs):
        class Provider:
            def plan(self, payload):
                return BrainResponse(
                    backend="hiyo-codex", model="fake-model", fallback=True,
                    error_kind="http", error="HTTP 400 from strategic provider",
                    http_status=400, http_error_code="invalid_request_error",
                    http_error_param="input",
                )
        return Provider()

    result = probe_brain_connection(
        {"openai_model": "fake-model",
         "openai_endpoint": "https://gateway.example/v1/responses"},
        "probe-secret", provider_factory=provider_factory)

    assert result["ok"] is False
    assert result["http_status"] == 400
    assert result["http_error_code"] == "invalid_request_error"
    assert result["http_error_param"] == "input"
    assert "HTTP 400" in result["message"]
    assert "probe-secret" not in json.dumps(result)
