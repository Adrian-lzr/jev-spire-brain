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
