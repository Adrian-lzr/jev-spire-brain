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
    "openai_model": "OPENAI_MODEL",
    "openai_endpoint": "OPENAI_BRAIN_ENDPOINT",
    "jev_backend": "JEV_BACKEND",
}
SECRET_KEYS = {
    "brain_api_key": "BRAIN_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
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
    return {
        "brain_backend": effective["brain_backend"],
        "brain_model": effective["openai_model"],
        "brain_endpoint": effective["openai_endpoint"],
        "openai_model": effective["openai_model"],
        "openai_endpoint": effective["openai_endpoint"],
        "jev_backend": effective["jev_backend"],
        "effective": effective,
        "secrets": {
            field: bool(os.environ.get(env_name) or values.get(env_name))
            for field, env_name in SECRET_KEYS.items()
        },
    }


def get_secret(path: Path, field: str) -> str:
    """Resolve a secret for a provider request without exposing it to clients."""
    env_name = SECRET_KEYS[field]
    if field == "brain_api_key":
        return (os.environ.get("BRAIN_API_KEY") or _values(path).get("BRAIN_API_KEY")
                or os.environ.get("OPENAI_API_KEY") or _values(path).get("OPENAI_API_KEY", ""))
    return os.environ.get(env_name) or _values(path).get(env_name, "")


def save_config(path: Path, data: dict) -> dict:
    """Update managed dotenv entries atomically while preserving other lines."""
    if not isinstance(data, dict):
        raise ValueError("配置格式无效")
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


def test_brain_connection(settings: dict, key: str) -> dict:
    if not key:
        return {"ok": False, "message": "尚未配置 GPT API Key"}
    endpoint = settings.get("openai_endpoint", "https://api.openai.com/v1/responses").strip()
    model = settings.get("openai_model", "gpt-4.1-mini").strip()
    if not endpoint.startswith("https://") or len(endpoint) > 512 or not model or len(model) > 128:
        return {"ok": False, "message": "模型地址或名称无效"}
    if "chat/completions" in endpoint:
        payload = {"model": model, "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 8}
    else:
        payload = {"model": model, "input": "Reply OK.", "max_output_tokens": 8}
    started = time.monotonic()
    try:
        status, body = _http_post(endpoint, key, payload, 15)
        if 200 <= status < 300 and isinstance(body, dict):
            return {"ok": True, "message": "GPT API 连接成功", "model": model,
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        return {"ok": False, "message": "GPT API 返回了无法识别的响应"}
    except urllib.error.HTTPError as exc:
        message = "API Key 或账户权限未通过" if exc.code in (401, 403) else f"服务返回 HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError):
        message = "网络连接失败或请求超时"
    except (ValueError, UnicodeError):
        message = "服务响应格式无效"
    return {"ok": False, "message": message,
            "latency_ms": int((time.monotonic() - started) * 1000)}


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
