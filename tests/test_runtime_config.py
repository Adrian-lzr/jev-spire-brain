"""Offline tests for effective provider configuration and trace redaction."""

from __future__ import annotations

import json
from pathlib import Path

from spirebrain.driver.trace import DecisionTrace
from spirebrain.runtime_config import resolve_runtime_config


def test_runtime_config_priority_and_public_fingerprint(tmp_path: Path):
    strategy = {"brain": {"backend": "openai", "model": "strategy-model"},
                "jev": {"backend": "mock"}}
    config = resolve_runtime_config(
        tmp_path,
        cli={"backend": "scripted", "brain_backend": "mock"},
        environ={"JEV_BACKEND": "openrouter", "OPENAI_MODEL": "env-model"},
        dotenv={"JEV_BACKEND": "official", "OPENAI_MODEL": "dotenv-model"},
        strategy=strategy,
    )
    assert config.jev_backend == "scripted"
    assert config.brain_backend == "mock"
    assert config.openai_model == "env-model"
    assert config.sources["jev_backend"] == "command_line"
    assert config.sources["openai_model"] == "process_environment"
    public = config.public_dict()
    assert public["config_id"] == config.config_id
    assert "api_key" not in json.dumps(public).lower()


def test_runtime_config_reads_strategy_and_dotenv(tmp_path: Path):
    (tmp_path / ".env").write_text("JEV_BACKEND=openrouter\n", encoding="utf-8")
    config = resolve_runtime_config(
        tmp_path,
        environ={},
        strategy={"brain": {"backend": "mock"}, "jev": {"backend": "official"}},
    )
    assert config.jev_backend == "openrouter"
    assert config.brain_backend == "mock"
    assert config.sources["jev_backend"] == "dotenv"


def test_decision_trace_is_bounded_and_redacts_credentials(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    trace = DecisionTrace(path, config_id="cfg-test")
    assert trace.record("decision", {
        "state_id": "s1", "source_type": "rule_fallback",
        "reason": "Authorization: Bearer super-secret",
        "command": {"command": "state"},
        "ignored_secret": "should not be included",
    })
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["config_id"] == "cfg-test"
    assert "ignored_secret" not in record
    assert "super-secret" not in json.dumps(record)
    assert "redacted" in record["reason"]


def test_decision_trace_keeps_current_identity_and_confidence_fields(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    trace = DecisionTrace(path, config_id="cfg-test")
    assert trace.record("advice", {
        "run_id": "run-1", "run_epoch": 3, "state_id": "state-1",
        "decision_id": "decision-1", "advice_revision": 2,
        "candidate_id": "combat:play:0:0", "brain_error": "temporary",
        "raw_model_confidence": 0.42, "local_confidence": 0.8,
        "selection_basis": "gpt_strategy", "combat_facts": {"lethal": True},
    })
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["run_epoch"] == 3
    assert record["advice_revision"] == 2
    assert record["candidate_id"] == "combat:play:0:0"
    assert record["raw_model_confidence"] == 0.42
    assert record["local_confidence"] == 0.8
    assert record["combat_facts"]["lethal"] is True


def test_brain_log_redacts_nested_provider_errors_and_plan_text(tmp_path: Path):
    from spirebrain.brain.gpt_client import LoggingStrategicClient
    from spirebrain.brain.protocol import BrainResponse, StrategicPlan

    class Provider:
        backend_name = "mock"

        def plan(self, payload):
            plan = StrategicPlan.from_dict({
                "plan_id": "p", "state_id": payload["state_id"],
                "run_id": payload["run_id"], "current_objective": "API key: super-secret",
                "long_term_goal": "survive", "priority": [],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {"note": "Bearer super-secret"},
                "next_steps": [], "replan_triggers": [], "reason": "ok",
                "uncertainty": "", "expires_after": 1,
            }, state_id=payload["state_id"], run_id=payload["run_id"])
            return BrainResponse(plan=plan, backend="mock", error="Authorization: Bearer super-secret")

    path = tmp_path / "brain_calls.jsonl"
    LoggingStrategicClient(Provider(), path).plan({"state_id": "s", "run_id": "r"})
    text = path.read_text(encoding="utf-8")
    assert "super-secret" not in text
    assert "[redacted]" in text


def test_redaction_handles_json_quoted_api_key_fields():
    from spirebrain.redaction import redact_text

    text = '{"api_key":"super-secret", "access_token": "other-secret"}'
    redacted = redact_text(text)
    assert "super-secret" not in redacted
    assert "other-secret" not in redacted


def test_openai_http_error_does_not_copy_provider_response_body():
    import io
    import urllib.error
    from spirebrain.brain.gpt_client import OpenAIStrategicClient

    body = b'{"error":"Authorization: Bearer super-secret"}'

    def opener(request, timeout):
        raise urllib.error.HTTPError("https://provider.test", 401, "no", None,
                                     io.BytesIO(body))

    result = OpenAIStrategicClient(api_key="test", opener=opener).plan(
        {"state_id": "s", "run_id": "r"})
    assert result.error == "HTTP 401 from strategic provider"
    assert "super-secret" not in result.error


def test_runtime_config_supports_mainland_openai_compatible_presets(tmp_path: Path):
    config = resolve_runtime_config(
        tmp_path,
        environ={"BRAIN_BACKEND": "deepseek"},
        strategy={"brain": {}, "jev": {"backend": "mock"}},
    )
    assert config.brain_backend == "deepseek"
    assert config.openai_model == "deepseek-chat"
    assert config.openai_endpoint.endswith("/v1/chat/completions")


def test_strategy_endpoint_and_budget_are_effective(tmp_path: Path):
    config = resolve_runtime_config(
        tmp_path,
        environ={},
        strategy={"brain": {"backend": "openai", "endpoint": "http://gateway.test/v1/chat/completions",
                             "decision_budget_ms": 1200, "max_model_calls": 2},
                  "jev": {"backend": "mock"}},
    )
    assert config.openai_endpoint == "http://gateway.test/v1/chat/completions"
    assert config.decision_budget_ms == 1200
    assert config.max_model_calls == 2
    assert config.jev_budget_ms == 2500
    assert config.jev_max_retries == 1


def test_compatible_base_endpoint_gets_chat_route_and_jev_endpoint_is_effective(tmp_path: Path):
    config = resolve_runtime_config(
        tmp_path,
        environ={"OPENAI_BRAIN_ENDPOINT": "https://gateway.test/v1",
                 "OPENROUTER_ENDPOINT": "https://openrouter.test"},
        strategy={"brain": {}, "jev": {"backend": "openrouter"}},
    )
    assert config.openai_endpoint == "https://gateway.test/v1/chat/completions"
    assert config.jev_endpoint == "https://openrouter.test"


def test_dashboard_public_config_distinguishes_saved_and_effective(tmp_path: Path):
    from spirebrain.overlay.config import get_public_config
    (tmp_path / ".env").write_text("BRAIN_BACKEND=mock\nJEV_BACKEND=mock\n", encoding="utf-8")
    public = get_public_config(tmp_path / ".env")
    assert public["saved"]["brain_backend"] == "mock"
    assert public["effective"]["brain_backend"] == "mock"
    assert all(isinstance(value, bool) for value in public["secrets"].values())
