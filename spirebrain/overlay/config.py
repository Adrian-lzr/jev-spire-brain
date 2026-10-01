"""Local-only dashboard configuration helpers.

Secrets are read from or written to the project .env file, but are never
returned to the browser or included in diagnostics.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from spirebrain.runtime_config import read_dotenv, resolve_runtime_config


CONFIG_KEYS = {
    "brain_backend": "BRAIN_BACKEND",
    "brain_model": "BRAIN_MODEL",
    "brain_endpoint": "BRAIN_ENDPOINT",
    "brain_reasoning_effort": "BRAIN_REASONING_EFFORT",
    "decision_budget_ms": "DECISION_BUDGET_MS",
    "max_model_calls": "MAX_MODEL_CALLS",
    "jev_budget_ms": "JEV_BUDGET_MS",
    "jev_max_retries": "JEV_MAX_RETRIES",
    "openai_model": "OPENAI_MODEL",
    "openai_endpoint": "OPENAI_BRAIN_ENDPOINT",
    "jev_backend": "JEV_BACKEND",
}
SECRET_KEYS = {
    "brain_api_key": "BRAIN_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "codex_api_key": "CODEX_API_KEY",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "typesafe_api_key": "TYPESAFE_API_KEY",
    "cloudflare_api_token": "CLOUDFLARE_API_TOKEN",
    "cloudflare_account_id": "CLOUDFLARE_ACCOUNT_ID",
}
MAX_SECRET_LENGTH = 4096
MAX_CONFIG_BYTES = 16 * 1024


def _read_env_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _values(path: Path) -> dict[str, str]:
    return read_dotenv(path)


def get_public_config(path: Path) -> dict:
    values = _values(path)
    effective = resolve_runtime_config(path.parent, dotenv=values).public_dict()
    # Return managed non-secret values separately from the resolved runtime
    # snapshot. Environment variables and CLI flags may override these values;
    # the dashboard must make that distinction visible instead of implying a
    # saved form is already active in the game process.
    saved = {field: values.get(env_name, "")
             for field, env_name in CONFIG_KEYS.items()}
    return {
        "brain_backend": effective["brain_backend"],
        "brain_model": effective["openai_model"],
        "brain_endpoint": effective["openai_endpoint"],
        "openai_model": effective["openai_model"],
        "openai_endpoint": effective["openai_endpoint"],
        "jev_backend": effective["jev_backend"],
        "effective": effective,
        "saved": saved,
        "restart_required": bool(effective.get("restart_required", True)),
        "secrets": {
            field: bool(os.environ.get(env_name) or values.get(env_name))
            for field, env_name in SECRET_KEYS.items()
        },
    }


def get_secret(path: Path, field: str, *, endpoint: str | None = None) -> str:
    """Resolve a secret for a provider request without exposing it to clients."""
    if field == "brain_api_key":
        cli = {"brain_endpoint": endpoint} if endpoint else None
        return resolve_runtime_config(
            path.parent, cli=cli, dotenv=_values(path)).brain_api_key
    env_name = SECRET_KEYS[field]
    return os.environ.get(env_name) or _values(path).get(env_name, "")


def save_config(path: Path, data: dict) -> dict:
    """Update managed dotenv entries atomically while preserving other lines."""
    if not isinstance(data, dict):
        raise ValueError("配置格式无效")
    # The dashboard historically used the legacy ``openai_*`` field names,
    # while the live launcher resolves the canonical ``brain_*`` aliases first.
    # Normalize the legacy form before writing so saving the UI cannot leave a
    # stale model/endpoint in BRAIN_MODEL/BRAIN_ENDPOINT.
    data = dict(data)
    if "brain_model" not in data and "openai_model" in data:
        data["brain_model"] = data["openai_model"]
    if "brain_endpoint" not in data and "openai_endpoint" in data:
        data["brain_endpoint"] = data["openai_endpoint"]
    values: dict[str, str] = {}
    for field, env_name in CONFIG_KEYS.items():
        if field in data:
            value = data[field]
            if not isinstance(value, str) or len(value) > 512 or "\n" in value or "\r" in value:
                raise ValueError("配置项格式无效")
            values[env_name] = value.strip()
    clear = data.get("clear_secrets", [])
    if not isinstance(clear, list) or any(k not in SECRET_KEYS for k in clear):
        raise ValueError("密钥操作无效")
    for field, env_name in SECRET_KEYS.items():
        value = data.get(field)
        if value is not None and value != "":
            if not isinstance(value, str) or len(value) > MAX_SECRET_LENGTH or "\n" in value or "\r" in value:
                raise ValueError("密钥格式无效")
            values[env_name] = value.strip()
        elif field in clear:
            values[env_name] = ""

    old_lines = _read_env_file(path)
    emitted: set[str] = set()
    new_lines: list[str] = []
    for raw in old_lines:
        line = raw.strip()
        key = line.partition("=")[0].strip().removeprefix("export ").strip()
        if key in values:
            if key not in emitted:
                new_lines.append(f"{key}={values[key]}")
                emitted.add(key)
            continue
        new_lines.append(raw)
    for key, value in values.items():
        if key not in emitted:
            new_lines.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return get_public_config(path)


def _http_post(url: str, api_key: str, payload: dict, timeout: float) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "jev-spire-brain/config-test"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read(64 * 1024).decode("utf-8"))
        return response.status, body


def test_brain_connection(settings: dict, key: str, *, provider_factory=None) -> dict:
    if not key:
        return {"ok": False, "message": "尚未配置 GPT API Key"}
    endpoint = settings.get("openai_endpoint", "https://api.openai.com/v1/responses").strip()
    model = settings.get("openai_model", "gpt-4.1-mini").strip()
    if not endpoint.startswith("https://") or len(endpoint) > 512 or not model or len(model) > 128:
        return {"ok": False, "message": "模型地址或名称无效"}
    started = time.monotonic()
    try:
        if provider_factory is None:
            from spirebrain.brain.gpt_client import OpenAIStrategicClient

            provider_factory = OpenAIStrategicClient
        client = provider_factory(
            api_key=key, model=model, endpoint=endpoint,
            reasoning_effort=settings.get("reasoning_effort"),
            timeout_ms=15_000, max_output_tokens=900, max_calls=1,
        )
        state_id = "dashboard-provider-probe-state"
        run_id = "dashboard-provider-probe-run"
        candidate_id = "probe:wait"
        response = client.plan({
            "run_id": run_id,
            "run_epoch": 0,
            "decision_id": "dashboard-provider-probe-decision",
            "state_id": state_id,
            "trigger": "provider_probe",
            "max_plan_steps": 2,
            "state": {"screen_type": "MAP", "character": "IRONCLAD",
                      "act": 1, "floor": 1, "current_hp": 50,
                      "max_hp": 80, "gold": 99},
            "candidates": [{
                "candidate_id": candidate_id,
                "candidate_signature": "dashboard-provider-probe",
                "kind": "wait", "label": "Wait for the next state",
                "command": {"command": "wait"}, "legal": True,
            }],
        })
        latency = getattr(response, "latency_ms", 0) or int(
            (time.monotonic() - started) * 1000)
        plan = getattr(response, "plan", None)
        preferences = set(getattr(plan, "preferred_candidates", ()) or ())
        preferences.update(getattr(plan, "avoid_candidates", ()) or ())
        valid = bool(
            plan is not None and not getattr(response, "fallback", True)
            and plan.state_id == state_id and plan.run_id == run_id
            and preferences <= {candidate_id}
        )
        if valid:
            return {"ok": True, "message": "已收到可解析的战略计划",
                    "model": getattr(response, "model", "") or model,
                    "latency_ms": latency}
        error_kind = str(getattr(response, "error_kind", "") or "invalid_plan")
        messages = {
            "missing_key": "尚未配置 GPT API Key",
            "auth": "API Key 或账户权限未通过",
            "rate_limit": "模型服务限流，请稍后再试",
            "timeout": "模型请求超时",
            "network": "无法连接模型服务",
            "parse_or_validation": "服务已响应，但没有返回有效战略计划",
            "budget_exhausted": "战略请求预算已耗尽",
        }
        diagnostic_parts = []
        for field in ("http_status", "http_error_code", "http_error_param"):
            value = getattr(response, field, None)
            if isinstance(value, int) and not isinstance(value, bool):
                diagnostic_parts.append(f"HTTP {value}")
            elif isinstance(value, str) and value:
                diagnostic_parts.append(f"{field.removeprefix('http_error_')}={value}")
        message = messages.get(error_kind, "模型请求失败，未获得有效战略计划")
        if diagnostic_parts:
            message += "（" + ", ".join(diagnostic_parts) + "）"
        result = {"ok": False, "message": message,
                  "error_kind": error_kind, "model": model, "latency_ms": latency}
        for field in ("http_status", "http_error_code", "http_error_param"):
            value = getattr(response, field, None)
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                result[field] = value
        return result
    except urllib.error.HTTPError as exc:
        error_kind = ("auth" if exc.code in (401, 403) else
                      "rate_limit" if exc.code == 429 else "http")
        message = ("API Key 或账户权限未通过" if error_kind == "auth" else
                   "模型服务限流，请稍后再试" if error_kind == "rate_limit" else
                   f"模型服务返回 HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError):
        error_kind = "timeout_or_network"
        message = "无法连接模型服务或请求超时"
    except Exception:
        error_kind = "probe_error"
        message = "战略计划测试失败，请检查模型地址、密钥和模型权限"
    return {"ok": False, "message": message, "error_kind": error_kind,
            "model": model, "latency_ms": int((time.monotonic() - started) * 1000)}


def test_jev_connection(backend: str, key: str, account_id: str = "") -> dict:
    if backend == "mock":
        return {"ok": True, "message": "Mock JEV 已就绪（离线模式）", "model": "mock", "latency_ms": 0}
    if backend not in ("openrouter", "official", "llm", "cloudflare"):
        return {"ok": False, "message": "当前 JEV 后端不支持此连接测试"}
    if backend == "cloudflare" and not account_id:
        return {"ok": False, "message": "尚未配置 Cloudflare Account ID"}
    if not key:
        return {"ok": False, "message": "尚未配置此 JEV 后端的 API Key"}
    started = time.monotonic()
    try:
        from spirebrain.jev_brain.client import NoulSpec
        if backend == "openrouter":
            from spirebrain.jev_brain.client_real import OpenRouterJevClient
            client = OpenRouterJevClient(api_key=key, timeout=15, max_retries=0)
        elif backend == "official":
            from spirebrain.jev_brain.client_real import OfficialJevClient
            client = OfficialJevClient(api_key=key, timeout=15, max_retries=0)
        elif backend == "llm":
            from spirebrain.jev_brain.openrouter_client import LlmStructuredClient
            client = LlmStructuredClient(api_key=key, timeout=15, max_retries=0)
        else:
            from spirebrain.jev_brain.client_real import CloudflareJevClient
            client = CloudflareJevClient(api_key=key, account_id=account_id,
                                         timeout=15, max_retries=0)
        client.ask({"purpose": "connection_test", "state": "API connectivity check"}, {
            "reachable": NoulSpec(instructions="Is the provided state an API connectivity check?")
        })
        return {"ok": True, "message": "JEV API 连接成功", "model": client.model,
                "latency_ms": int((time.monotonic() - started) * 1000)}
    except Exception as exc:  # Do not expose provider bodies or request headers.
        name = type(exc).__name__
        message = "JEV API Key 或账户权限未通过" if "401" in str(exc) or "403" in str(exc) else "JEV 请求失败，请检查网络、密钥和账户额度"
        return {"ok": False, "message": message, "error_type": name,
                "latency_ms": int((time.monotonic() - started) * 1000)}
