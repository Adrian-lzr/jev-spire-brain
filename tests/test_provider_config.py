"""Provider construction uses resolved local configuration without leaking secrets."""

from __future__ import annotations

import json
from pathlib import Path


def _brain_response():
    plan = {
        "plan_id": "p1", "state_id": "s1", "run_id": "r1",
        "current_objective": "先活下来", "long_term_goal": "通关",
        "priority": ["survive"], "preferred_candidates": [],
        "avoid_candidates": [], "resource_constraints": {},
        "next_steps": ["等待新的合法状态"],
        "replan_triggers": ["screen_change"], "reason": "测试响应",
        "uncertainty": "", "expires_after": 1,
    }
    return {"model": "test-model", "output_text": json.dumps(plan)}


def test_dotenv_brain_key_is_explicitly_passed_to_provider(tmp_path: Path, monkeypatch):
    """A game-launched process need not inherit the shell's secret environment."""
    from spirebrain.runtime_config import resolve_runtime_config

    key = "dotenv-test-brain-key"
    config = resolve_runtime_config(
        tmp_path,
        environ={},
        dotenv={
            "BRAIN_BACKEND": "openai",
            "BRAIN_API_KEY": key,
            "BRAIN_MODEL": "test-model",
            "BRAIN_ENDPOINT": "https://provider.test/v1/responses",
        },
        strategy={"brain": {}, "jev": {"backend": "mock"}},
    )
    assert config.brain_api_key == key
    assert config.brain_key_source == "dotenv"
    public = config.public_dict()
    assert public["brain_key_available"] is True
    assert key not in json.dumps(public)
    process_config = resolve_runtime_config(
        tmp_path,
        environ={"OPENAI_API_KEY": "process-key"},
        dotenv={"BRAIN_API_KEY": key},
        strategy={"brain": {}, "jev": {"backend": "mock"}},
    )
    assert process_config.brain_api_key == "process-key"
    assert process_config.brain_key_source == "process_environment"

    captured = {}

    class Provider:
        backend_name = "openai"
        async_required = False

        def plan(self, payload):
            raise AssertionError("construction test must not call the provider")

    def fake_factory(backend, **kwargs):
        captured.update(backend=backend, **kwargs)
        return Provider()

    monkeypatch.setattr("spirebrain.brain.orchestrator.get_strategic_brain", fake_factory)
    from spirebrain.brain.orchestrator import StrategicOrchestrator

    StrategicOrchestrator(
        backend=config.brain_backend,
        model=config.openai_model,
        endpoint=config.openai_endpoint,
        api_key=config.brain_api_key,
        async_planning=False,
    )
    assert captured["api_key"] == key
    assert "api_key" not in json.dumps(config.public_dict()).lower()


def test_agent_resolves_dotenv_key_before_constructing_strategic_client(
    tmp_path: Path, monkeypatch
):
    """The same explicit-key path is used by the CommunicationMod entry point."""
    from spirebrain.driver import agent as agent_module

    key = "agent-dotenv-brain-key"
    (tmp_path / ".env").write_text(
        f"BRAIN_BACKEND=openai\nBRAIN_API_KEY={key}\n",
        encoding="utf-8",
    )
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(json.dumps({
        "goal": "survive",
        "brain": {"backend": "openai"},
        "jev": {"backend": "mock"},
    }), encoding="utf-8")
    captured = {}

    class Provider:
        backend_name = "openai"
        async_required = False
        event_sink = None

        def plan(self, payload):
            raise AssertionError("provider should not be called during construction")

    def fake_factory(backend, **kwargs):
        captured.update(backend=backend, **kwargs)
        return Provider()

    monkeypatch.setattr(agent_module, "ROOT", tmp_path)
    monkeypatch.setattr("spirebrain.brain.orchestrator.get_strategic_brain", fake_factory)
    agent_module.SpireBrainAgent(
        strategy_path=strategy_path,
        jev_backend="mock",
        brain_backend="openai",
        log_dir=tmp_path / "logs",
    )
    assert captured["api_key"] == key


def test_dotenv_brain_key_reaches_fake_http_provider_without_real_network(tmp_path: Path):
    """The resolved credential is usable by the actual OpenAI-compatible client."""
    from spirebrain.brain.gpt_client import get_strategic_brain
    from spirebrain.runtime_config import resolve_runtime_config

    key = "offline-fake-brain-key"
    config = resolve_runtime_config(
        tmp_path,
        environ={},
        dotenv={"BRAIN_API_KEY": key},
        strategy={"brain": {}, "jev": {"backend": "mock"}},
    )
    seen = {}

    class Response:
        def read(self):
            return json.dumps(_brain_response()).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(request, timeout):
        seen["authorization"] = request.headers.get("Authorization")
        seen["body"] = request.data.decode("utf-8")
        return Response()

    client = get_strategic_brain(
        config.brain_backend,
        api_key=config.brain_api_key,
        model=config.openai_model,
        endpoint="https://provider.test/v1/responses",
        opener=opener,
        timeout_ms=1000,
    )
    result = client.plan({"state_id": "s1", "run_id": "r1"})
    assert result.error == ""
    assert result.plan is not None
    assert seen["authorization"] == f"Bearer {key}"
    assert key not in seen["body"]


def test_jev_dotenv_key_is_explicitly_passed_for_openrouter_and_official(
    tmp_path: Path, monkeypatch
):
    from spirebrain.jev_brain.client import get_client
    from spirebrain.jev_brain.client import NoulSpec
    from spirebrain.runtime_config import resolve_runtime_config

    class Response:
        def read(self):
            return json.dumps({
                "id": "offline-jev-request",
                "model": "jev-test",
                "answers": {"ok": {"type": "noul", "noul": 0.9}},
                "usage": {},
            }).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    for backend, env_key in (("openrouter", "OPENROUTER_API_KEY"),
                             ("official", "TYPESAFE_API_KEY")):
        key = f"{backend}-offline-key"
        config = resolve_runtime_config(
            tmp_path,
            environ={},
            dotenv={"JEV_BACKEND": backend, env_key: key},
            strategy={"brain": {"backend": "mock"}, "jev": {}},
        )
        assert config.jev_api_key == key
        assert config.jev_key_source == "dotenv"
        client = get_client(backend, api_key=config.jev_api_key,
                            endpoint="https://provider.test/systemone",
                            max_retries=0, total_budget_ms=1000)
        assert client.api_key == key
        seen = {}

        def opener(request, timeout):
            seen["authorization"] = request.headers.get("Authorization")
            seen["body"] = request.data.decode("utf-8")
            return Response()

        monkeypatch.setattr("spirebrain.jev_brain.client_real.urllib.request.urlopen", opener)
        result = client.ask({"screen": "COMBAT"}, {
            "ok": NoulSpec(instructions="synthetic request"),
        })
        assert result.error == ""
        assert result.answers["ok"].value == 0.9
        assert seen["authorization"] == f"Bearer {key}"
        assert key not in seen["body"]
        public = json.dumps(config.public_dict())
        assert key not in public
    assert "api_key" not in public.lower()
