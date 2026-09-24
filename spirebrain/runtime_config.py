"""Resolve runtime settings once for the launcher, agent and dashboard."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def read_dotenv(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        values[key] = value.strip().strip('"').strip("'")
    return values


def load_dotenv(path: str | Path, environ: dict[str, str] | None = None) -> int:
    """Load missing variables only; an existing process environment wins."""
    target = os.environ if environ is None else environ
    loaded = 0
    for key, value in read_dotenv(path).items():
        if key not in target:
            target[key] = value
            loaded += 1
    return loaded


@dataclass(frozen=True)
class RuntimeConfig:
    jev_backend: str
    brain_backend: str
    openai_model: str
    openai_endpoint: str
    timeout_ms: int
    max_output_tokens: int
    max_plan_steps: int
    memory_events: int
    fallback: str
    score_acceptance: str
    sources: dict[str, str]
    config_id: str

    def public_dict(self) -> dict:
        return {
            "jev_backend": self.jev_backend,
            "brain_backend": self.brain_backend,
            "openai_model": self.openai_model,
            "openai_endpoint": self.openai_endpoint,
            "timeout_ms": self.timeout_ms,
            "max_output_tokens": self.max_output_tokens,
            "max_plan_steps": self.max_plan_steps,
            "memory_events": self.memory_events,
            "fallback": self.fallback,
            "score_acceptance": self.score_acceptance,
            "sources": dict(self.sources),
            "config_id": self.config_id,
            "restart_required": True,
        }


def resolve_runtime_config(root: str | Path, *, cli: Mapping[str, str | None] | None = None,
                           environ: Mapping[str, str] | None = None,
                           dotenv: Mapping[str, str] | None = None,
                           strategy: dict | None = None) -> RuntimeConfig:
    """Priority: CLI > process environment > .env > strategy file > defaults."""
    root = Path(root)
    cli = dict(cli or {})
    env = os.environ if environ is None else environ
    dot = read_dotenv(root / ".env") if dotenv is None else dotenv
    if strategy is None:
        strategy_path = root / "config" / "strategy.json"
        try:
            strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            strategy = {}
    brain = strategy.get("brain", {}) if isinstance(strategy, dict) else {}
    jev = strategy.get("jev", {}) if isinstance(strategy, dict) else {}
    brain = brain if isinstance(brain, dict) else {}
    jev = jev if isinstance(jev, dict) else {}
    sources: dict[str, str] = {}

    def pick(name: str, env_name: str, file_value, default: str,
             *, cli_name: str | None = None) -> str:
        for source, values, key in (
            ("command_line", cli, cli_name or name),
            ("process_environment", env, env_name),
            ("dotenv", dot, env_name),
        ):
            value = values.get(key)
            if value is not None and str(value).strip():
                sources[name] = source
                return str(value).strip()
        if file_value is not None and str(file_value).strip():
            sources[name] = "strategy.json"
            return str(file_value).strip()
        sources[name] = "default"
        return default

    has_openrouter_key = bool(
        str(env.get("OPENROUTER_API_KEY", "") or dot.get("OPENROUTER_API_KEY", "")).strip()
    )
    jev_default = str(jev.get("backend") or ("openrouter" if has_openrouter_key else "mock"))
    jev_backend = pick("jev_backend", "JEV_BACKEND", jev.get("backend"), jev_default,
                       cli_name="backend").lower()
    brain_backend = pick("brain_backend", "BRAIN_BACKEND", brain.get("backend"), "openai",
                         cli_name="brain_backend").lower()
    openai_model = pick("openai_model", "OPENAI_MODEL", brain.get("model"), "gpt-4.1-mini")
    openai_endpoint = pick("openai_endpoint", "OPENAI_BRAIN_ENDPOINT", None,
                           "https://api.openai.com/v1/responses")

    def number(name: str, env_name: str, file_value, default: int,
               minimum: int, maximum: int) -> int:
        raw = pick(name, env_name, file_value, str(default))
        try:
            return max(minimum, min(maximum, int(raw)))
        except (TypeError, ValueError):
            sources[name] = "default"
            return default

    timeout_ms = number("timeout_ms", "BRAIN_TIMEOUT_MS", brain.get("timeout_ms"),
                        6000, 500, 120000)
    max_output_tokens = number("max_output_tokens", "BRAIN_MAX_OUTPUT_TOKENS",
                               brain.get("max_output_tokens"), 900, 128, 8192)
    max_plan_steps = number("max_plan_steps", "BRAIN_MAX_PLAN_STEPS",
                            brain.get("max_plan_steps"), 5, 2, 5)
    memory_events = number("memory_events", "BRAIN_MEMORY_EVENTS",
                           brain.get("memory_events"), 20, 1, 200)
    fallback = pick("fallback", "BRAIN_FALLBACK", brain.get("fallback"), "jev_rules")
    score_acceptance = pick("score_acceptance", "JEV_SCORE_ACCEPTANCE",
                            jev.get("score_acceptance"), "argmax")
    if score_acceptance not in {"argmax", "margin"}:
        score_acceptance = "argmax"
        sources["score_acceptance"] = "default"

    public_values = {
        "jev_backend": jev_backend,
        "brain_backend": brain_backend,
        "openai_model": openai_model,
        "openai_endpoint": openai_endpoint,
        "timeout_ms": timeout_ms,
        "max_output_tokens": max_output_tokens,
        "max_plan_steps": max_plan_steps,
        "memory_events": memory_events,
        "fallback": fallback,
        "score_acceptance": score_acceptance,
    }
    config_id = hashlib.sha256(json.dumps(
        {"values": public_values, "sources": sources}, sort_keys=True,
        separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:16]
    return RuntimeConfig(**public_values, sources=sources, config_id=config_id)
