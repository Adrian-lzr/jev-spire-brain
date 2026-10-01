"""Resolve runtime settings once for the launcher, agent and dashboard."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


# Common mainland providers expose an OpenAI-compatible chat endpoint.  These
# defaults are only used when the user selects a backend and leaves model or
# endpoint blank; explicit BRAIN_* / OPENAI_* values always win.
BRAIN_PROVIDER_PRESETS = {
    "hiyo-codex": ("gpt-6-sol", "https://codex.hiyo.top/v1/responses"),
    "deepseek": ("deepseek-chat", "https://api.deepseek.com/v1/chat/completions"),
    "qwen": ("qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
    "tongyi": ("qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
    "zhipu": ("glm-4.5", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
    "glm": ("glm-4.5", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
    "moonshot": ("kimi-k2-0711-preview", "https://api.moonshot.cn/v1/chat/completions"),
    "kimi": ("kimi-k2-0711-preview", "https://api.moonshot.cn/v1/chat/completions"),
    "siliconflow": ("deepseek-ai/DeepSeek-V3", "https://api.siliconflow.cn/v1/chat/completions"),
    "doubao": ("doubao-seed-1-6-250615", "https://ark.cn-beijing.volces.com/api/v3/chat/completions"),
}


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
    brain_reasoning_effort: str
    # Secret provider material is resolved once at the process boundary and
    # passed explicitly to the provider.  It is intentionally excluded from
    # ``public_dict`` and from ``config_id``; only availability/source are
    # exposed for diagnostics.
    brain_api_key: str = field(repr=False)
    brain_key_source: str
    jev_api_key: str = field(repr=False)
    jev_key_source: str
    jev_endpoint: str
    timeout_ms: int
    max_output_tokens: int
    max_plan_steps: int
    memory_events: int
    fallback: str
    score_acceptance: str
    decision_budget_ms: int
    max_model_calls: int
    jev_budget_ms: int
    jev_max_retries: int
    sources: dict[str, str]
    config_id: str

    def public_dict(self) -> dict:
        return {
            "jev_backend": self.jev_backend,
            "brain_backend": self.brain_backend,
            "openai_model": self.openai_model,
            "openai_endpoint": self.openai_endpoint,
            "brain_reasoning_effort": self.brain_reasoning_effort,
            "brain_key_available": bool(self.brain_api_key),
            "brain_key_source": self.brain_key_source,
            "jev_key_available": bool(self.jev_api_key),
            "jev_key_source": self.jev_key_source,
            "jev_endpoint": self.jev_endpoint,
            "timeout_ms": self.timeout_ms,
            "max_output_tokens": self.max_output_tokens,
            "max_plan_steps": self.max_plan_steps,
            "memory_events": self.memory_events,
            "fallback": self.fallback,
            "score_acceptance": self.score_acceptance,
            "decision_budget_ms": self.decision_budget_ms,
            "max_model_calls": self.max_model_calls,
            "jev_budget_ms": self.jev_budget_ms,
            "jev_max_retries": self.jev_max_retries,
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

    def pick_alias(name: str, env_names: tuple[str, ...], file_value,
                   default: str) -> str:
        """Pick the first configured alias while retaining its source."""
        for env_name in env_names:
            value = pick(name, env_name, None, "")
            if value:
                return value
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
    jev_endpoint = pick_alias("jev_endpoint", ("JEV_ENDPOINT", "OPENROUTER_ENDPOINT"),
                              jev.get("endpoint"), "https://openrouter.ai/api/v1/systemone")
    if jev_backend == "openrouter":
        base = jev_endpoint.rstrip("/")
        if base in {"https://openrouter.ai", "https://openrouter.ai/api/v1"}:
            jev_endpoint = "https://openrouter.ai/api/v1/systemone"
        elif base.endswith("/api/v1"):
            jev_endpoint = base + "/systemone"
    # JEV credentials are selected for the resolved backend.  Keep the source
    # metadata public, but never expose the key itself through RuntimeConfig's
    # public representation or its fingerprint.
    jev_key_aliases = {
        "openrouter": ("OPENROUTER_API_KEY",),
        "llm": ("OPENROUTER_API_KEY",),
        "official": ("TYPESAFE_API_KEY",),
        "cloudflare": ("CLOUDFLARE_API_TOKEN",),
    }.get(jev_backend, ())
    jev_api_key = ""
    jev_key_source = "default"
    for source, values in (("process_environment", env), ("dotenv", dot)):
        for key_name in jev_key_aliases:
            value = values.get(key_name)
            if value is not None and str(value).strip():
                jev_api_key = str(value).strip()
                jev_key_source = source
                break
        if jev_api_key:
            break
    brain_backend = pick("brain_backend", "BRAIN_BACKEND", brain.get("backend"), "openai",
                         cli_name="brain_backend").lower()
    preset_model, preset_endpoint = BRAIN_PROVIDER_PRESETS.get(
        brain_backend, ("gpt-4.1-mini", "https://api.openai.com/v1/responses")
    )
    openai_model = pick_alias("openai_model", ("BRAIN_MODEL", "OPENAI_MODEL"),
                              brain.get("model"), preset_model)
    openai_endpoint = pick_alias(
        "openai_endpoint", ("BRAIN_ENDPOINT", "OPENAI_BRAIN_ENDPOINT"),
        brain.get("endpoint"),
        preset_endpoint,
    )
    # Explicit CLI aliases are applied after the environment aliases so a
    # launcher can defeat stale inherited process variables without exposing a
    # secret in the command line.
    for key, value in (("openai_model", cli.get("brain_model")),
                       ("openai_endpoint", cli.get("brain_endpoint")),
                       ("jev_endpoint", cli.get("jev_endpoint"))):
        if value is not None and str(value).strip():
            if key == "openai_model": openai_model = str(value).strip()
            elif key == "openai_endpoint": openai_endpoint = str(value).strip()
            else: jev_endpoint = str(value).strip()
            sources[key] = "command_line"
    # A base /v1 URL is ambiguous. Preserve the official Responses API route;
    # compatible gateways use Chat Completions unless an explicit route was
    # configured. The provider adapter builds the matching wire shape.
    parsed_endpoint = urlsplit(openai_endpoint)
    if parsed_endpoint.path.rstrip("/").lower().endswith("/v1"):
        route = ("/responses" if parsed_endpoint.hostname == "api.openai.com"
                 else "/chat/completions")
        openai_endpoint = urlunsplit((
            parsed_endpoint.scheme, parsed_endpoint.netloc,
            parsed_endpoint.path.rstrip("/") + route,
            parsed_endpoint.query, parsed_endpoint.fragment,
        ))

    reasoning_effort = pick_alias(
        "brain_reasoning_effort", ("BRAIN_REASONING_EFFORT",),
        brain.get("reasoning_effort"), "",
    ).lower()
    if cli.get("brain_reasoning_effort") is not None and str(cli["brain_reasoning_effort"]).strip():
        reasoning_effort = str(cli["brain_reasoning_effort"]).strip().lower()
        sources["brain_reasoning_effort"] = "command_line"
    if reasoning_effort not in {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        reasoning_effort = ""
        sources["brain_reasoning_effort"] = "default"

    # The Codex gateway must not receive a generic BRAIN_API_KEY that may be
    # scoped to another provider. It uses its own credential alias instead.
    host = (urlsplit(openai_endpoint).hostname or "").lower()
    brain_key_aliases = (("CODEX_API_KEY", "OPENAI_API_KEY")
                         if host == "codex.hiyo.top" or host.endswith(".codex.hiyo.top")
                         else ("BRAIN_API_KEY", "OPENAI_API_KEY"))
    brain_api_key = ""
    brain_key_source = "default"
    for source, values in (("process_environment", env), ("dotenv", dot)):
        for key_name in brain_key_aliases:
            value = values.get(key_name)
            if value is not None and str(value).strip():
                brain_api_key = str(value).strip()
                brain_key_source = source
                break
        if brain_api_key:
            break

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
                               brain.get("max_output_tokens"), 1400, 128, 8192)
    max_plan_steps = number("max_plan_steps", "BRAIN_MAX_PLAN_STEPS",
                            brain.get("max_plan_steps"), 5, 2, 5)
    memory_events = number("memory_events", "BRAIN_MEMORY_EVENTS",
                           brain.get("memory_events"), 20, 1, 200)
    fallback = pick("fallback", "BRAIN_FALLBACK", brain.get("fallback"), "jev_rules")
    score_acceptance = pick("score_acceptance", "JEV_SCORE_ACCEPTANCE",
                            jev.get("score_acceptance"), "argmax")
    decision_budget_ms = number("decision_budget_ms", "DECISION_BUDGET_MS",
                                brain.get("decision_budget_ms", brain.get("timeout_ms")),
                                6000, 250, 120000)
    max_model_calls = number("max_model_calls", "MAX_MODEL_CALLS",
                             brain.get("max_model_calls"), 4, 0, 16)
    jev_budget_ms = number("jev_budget_ms", "JEV_BUDGET_MS",
                           jev.get("total_budget_ms"), 2500, 250, 120000)
    jev_max_retries = number("jev_max_retries", "JEV_MAX_RETRIES",
                             jev.get("max_retries"), 1, 0, 3)
    if score_acceptance not in {"argmax", "margin"}:
        score_acceptance = "argmax"
        sources["score_acceptance"] = "default"

    public_values = {
        "jev_backend": jev_backend,
        "brain_backend": brain_backend,
        "openai_model": openai_model,
        "openai_endpoint": openai_endpoint,
        "brain_reasoning_effort": reasoning_effort,
        "brain_api_key": brain_api_key,
        "brain_key_source": brain_key_source,
        "jev_api_key": jev_api_key,
        "jev_key_source": jev_key_source,
        "jev_endpoint": jev_endpoint,
        "timeout_ms": timeout_ms,
        "max_output_tokens": max_output_tokens,
        "max_plan_steps": max_plan_steps,
        "memory_events": memory_events,
        "fallback": fallback,
        "score_acceptance": score_acceptance,
        "decision_budget_ms": decision_budget_ms,
        "max_model_calls": max_model_calls,
        "jev_budget_ms": jev_budget_ms,
        "jev_max_retries": jev_max_retries,
    }
    # Never hash the secret itself: besides being unnecessary, doing so would
    # make a supposedly public config fingerprint depend on credential bytes.
    config_identity = {key: value for key, value in public_values.items()
                       if key not in {"brain_api_key", "jev_api_key"}}
    config_id = hashlib.sha256(json.dumps(
        {"values": config_identity, "sources": sources}, sort_keys=True,
        separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:16]
    return RuntimeConfig(**public_values, sources=sources, config_id=config_id)
