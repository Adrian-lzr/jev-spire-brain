"""Provider adapters for the strategic brain.

The OpenAI adapter uses only the Python standard library, matching the existing
JEV client.  It accepts both the Responses-shaped payload used by OpenAI and a
chat-completions compatible endpoint so local gateways can be tested without a
second implementation.
"""

from __future__ import annotations

import json
import os
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
import threading
from typing import Any
from urllib.parse import urlsplit
import re

from .protocol import BrainResponse, PlanValidationError, StrategicPlan
from spirebrain.redaction import redact_text, redact_value
from spirebrain.logging_utils import append_bounded
from spirebrain.runtime_budget import DecisionBudget, current_budget


PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "plan_id": {"type": "string"},
        "state_id": {"type": "string"},
        "run_id": {"type": "string"},
        "current_objective": {"type": "string"},
        "long_term_goal": {"type": "string"},
        "priority": {"type": "array", "items": {"type": "string"}},
        "preferred_candidates": {"type": "array", "items": {"type": "string"}},
        "avoid_candidates": {"type": "array", "items": {"type": "string"}},
        "resource_constraints": {"type": "object", "additionalProperties": True},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "replan_triggers": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "uncertainty": {"type": "string"},
        "expires_after": {"type": ["integer", "null"]},
    },
    "required": [
        "plan_id", "state_id", "run_id", "current_objective", "long_term_goal",
        "priority", "preferred_candidates", "avoid_candidates",
        "resource_constraints", "next_steps", "replan_triggers", "reason",
        "uncertainty", "expires_after",
    ],
}


SYSTEM_PROMPT = """你是 Slay the Spire 的战略规划大脑。
你只负责整局目标、资源约束和候选优先级，不直接生成游戏命令、鼠标坐标、卡牌索引或协议动词。
只能从候选列表中引用 candidate_id。所有动作最终由本地合法性层和 JEV 战术层确认。
当前状态会带有一个保守的打法 profile（力量成长、消耗引擎、格挡堆叠、自伤收益、异常牌转化或自适应端口）。
只有在卡组信号足够时才延续该打法；信号冲突或不足时选择自适应端口，优先解决输出、防御、运转三端中最弱的一端。
不要为了凑流派牺牲当前战斗生存、HP 预算、金币保留或路线可行性。
这是跨多个局部动作复用的阶段策略，不是逐张牌或逐次轮询的动作脚本。replan_triggers 只描述真正的战略变化；expires_after 表示计划视野，不代表每执行若干个动作就必须再次调用模型。
优先保护明确的生存约束，解释只写简短、可展示的理由，不输出隐藏思维链。
返回严格符合给定 JSON Schema 的短计划。"""

MAX_STRATEGIC_RESPONSE_BYTES = 256 * 1024
MAX_HTTP_ERROR_BYTES = 8 * 1024
_SAFE_HTTP_DETAIL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/\[\]{}-]{0,127}$")


def _safe_http_detail(value: Any) -> str | None:
    """Return a bounded, non-secret provider error token.

    Provider error bodies are untrusted and may echo request fragments or
    credentials.  Only compact identifier-like ``code``/``param`` values are
    retained; messages and the raw body are deliberately discarded.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or not _SAFE_HTTP_DETAIL.fullmatch(value):
        return None
    return value


def _http_error_details(exc: urllib.error.HTTPError) -> tuple[str | None, str | None]:
    """Extract safe ``code`` and ``param`` fields from a bounded HTTP body."""
    try:
        raw = exc.read(MAX_HTTP_ERROR_BYTES + 1)
    except (OSError, ValueError, AttributeError):
        return None, None
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_HTTP_ERROR_BYTES:
        return None, None
    try:
        decoded = json.loads(bytes(raw).decode("utf-8", "replace"))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None, None
    error = decoded.get("error") if isinstance(decoded, dict) else None
    if not isinstance(error, dict):
        error = decoded if isinstance(decoded, dict) else {}
    return _safe_http_detail(error.get("code")), _safe_http_detail(error.get("param"))


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False)
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _json_candidates(value: Any) -> list[str]:
    """Return bounded JSON-looking fragments from a gateway text response.

    Compatible gateways frequently prepend a ``<think>``/Markdown wrapper or
    append a short sentence around the actual object.  Taking the first ``{``
    and last ``}`` is unsafe when the wrapper itself contains braces, so scan
    each opening brace with ``raw_decode`` and retain only complete objects.
    The result is still parsed and validated by ``StrategicPlan`` below.
    """
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    if not text:
        return []
    candidates: list[str] = [text]
    # Remove common hidden-reasoning wrappers before looking for a plan.  We do
    # not expose the removed text in logs or UI; it is only an input cleanup.
    for tag in ("think", "analysis", "reasoning"):
        open_tag, close_tag = f"<{tag}>", f"</{tag}>"
        while open_tag in text.lower() and close_tag in text.lower():
            lower = text.lower()
            start = lower.find(open_tag)
            end = lower.find(close_tag, start + len(open_tag))
            if end < 0:
                break
            text = (text[:start] + text[end + len(close_tag):]).strip()
    if text and text not in candidates:
        candidates.append(text)
    decoder = json.JSONDecoder()
    fragments: list[str] = []
    for source in list(candidates):
        for index, char in enumerate(source):
            if char != "{":
                continue
            try:
                _, end = decoder.raw_decode(source[index:])
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            fragment = source[index:index + end]
            if fragment not in fragments:
                fragments.append(fragment)
    # Prefer a complete object first, then the extracted fragments.
    return candidates + [item for item in fragments if item not in candidates]


def _unicode_safe_json(value: Any) -> Any:
    """Normalize gateway JSON without passing legacy-encoded text downstream."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8", "replace")
    if isinstance(value, list):
        return [_unicode_safe_json(item) for item in value]
    if isinstance(value, dict):
        return {_unicode_safe_json(key): _unicode_safe_json(item)
                for key, item in value.items()}
    return value


def _extract_text(body: dict) -> str:
    output_text = body.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = body.get("output") or []
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or []
        if isinstance(content, str) and content.strip():
            chunks.append(content)
            continue
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
            continue
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    if chunks:
        return "".join(chunks)
    choices = body.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
            content = content.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            if parts:
                return "".join(parts)
        if isinstance(content, str) and content.strip():
            return content
        # A few compatible gateways expose the completion under ``text`` or a
        # tool/function argument instead of message.content.  These are still
        # provider output, never executable commands.
        for key in ("text", "reasoning_content"):
            candidate = message.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments.strip():
                return arguments
        function_call = message.get("function_call") or {}
        arguments = function_call.get("arguments")
        if isinstance(arguments, str) and arguments.strip():
            return arguments
        text = choices[0].get("text")
        if isinstance(text, str) and text.strip():
            return text
    raise ValueError("模型响应中没有文本内容")


def _extract_texts(body: dict) -> list[str]:
    """Collect alternate text channels while preserving parser compatibility."""
    texts: list[str] = []
    try:
        first = _extract_text(body)
    except ValueError:
        first = ""
    if first:
        texts.append(first)
    choices = body.get("choices") or []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        for key in ("content", "text", "reasoning_content"):
            value = message.get(key)
            if isinstance(value, str) and value.strip() and value not in texts:
                texts.append(value)
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                arguments = (call.get("function") or {}).get("arguments")
                if isinstance(arguments, str) and arguments.strip() and arguments not in texts:
                    texts.append(arguments)
    return texts


def _unwrap_plan(data: Any) -> Any:
    """Accept common gateway wrappers while keeping the plan schema strict."""
    if not isinstance(data, dict):
        return data
    for key in ("plan", "strategic_plan", "result", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            # Only unwrap when the nested object looks like our plan.  A model
            # cannot use this convenience to smuggle arbitrary commands past
            # StrategicPlan.from_dict's unknown-field check.
            if "plan_id" in nested or "current_objective" in nested or "steps" in nested:
                return nested
    return data


def _coerce_steps_plan(data: Any, *, state_id: str, run_id: str,
                       max_steps: int) -> Any:
    """Normalize the compact ``steps`` shape used by several Chinese gateways.

    The adapter copies only candidate IDs and display reasons.  It never copies
    a provider command, index, or free-form action into the execution layer.
    Full StrategicPlan responses continue through the strict schema unchanged.
    """
    if not isinstance(data, dict):
        return data
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        # Some gateways reserve ``plan`` for a list of next actions.
        raw_steps = data.get("plan")
    if not isinstance(raw_steps, list):
        return data
    steps = [item for item in raw_steps if isinstance(item, dict)][:max_steps]
    preferred: list[str] = []
    reasons: list[str] = []
    for item in steps:
        candidate = item.get("candidate_id")
        if isinstance(candidate, str) and candidate.strip() and candidate.strip() not in preferred:
            preferred.append(candidate.strip()[:240])
        reason = item.get("reason")
        if isinstance(reason, str) and reason.strip():
            reasons.append(reason.strip()[:500])
    objective = data.get("current_objective") or data.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        objective = reasons[0] if reasons else "按当前合法候选推进"
    return {
        "plan_id": str(data.get("plan_id") or f"gateway-{state_id[:12]}"),
        "state_id": state_id,
        "run_id": run_id,
        "current_objective": objective[:500],
        "long_term_goal": str(data.get("long_term_goal") or "完成当前局面并保留资源")[:500],
        "priority": ["survive", "follow_strategy", "advance"],
        "preferred_candidates": preferred,
        "avoid_candidates": [],
        "resource_constraints": data.get("resource_constraints")
        if isinstance(data.get("resource_constraints"), dict) else {},
        "next_steps": preferred[:max_steps] or ["等待新的合法状态"],
        "replan_triggers": ["screen_change", "hp_change", "gold_change"],
        "reason": reasons[0] if reasons else "模型返回了当前候选步骤，已由本地合法性层校验。",
        "uncertainty": str(data.get("uncertainty") or "")[:300],
        "expires_after": 1,
    }


class UnavailableStrategicClient:
    # A disabled backend has no I/O.  Keeping this explicit lets the
    # orchestrator answer synchronously in tests and in the JEV-only mode.
    async_required = False

    def __init__(self, reason: str = "未配置 OPENAI_API_KEY",
                 backend_name: str = "unavailable") -> None:
        self.reason = reason
        self.backend_name = backend_name

    def plan(self, payload: dict) -> BrainResponse:
        return BrainResponse(backend=self.backend_name, error=self.reason, fallback=True)


class MockStrategicClient:
    """Deterministic planner used by tests and offline demos."""

    backend_name = "mock"
    async_required = False

    def __init__(self, preferred: list[str] | None = None,
                 objective: str = "先保证当前局面可控") -> None:
        self.preferred = list(preferred or [])
        self.objective = objective
        self.calls = 0
        self.payloads: list[dict] = []

    def plan(self, payload: dict) -> BrainResponse:
        self.calls += 1
        self.payloads.append(payload)
        candidates = payload.get("candidates") or []
        ids = [str(c.get("candidate_id")) for c in candidates if isinstance(c, dict)]
        preferred = [x for x in self.preferred if x in ids]
        if not preferred and ids:
            preferred = [ids[0]]
        state_id = str(payload.get("state_id", ""))
        run_id = str(payload.get("run_id", "local"))
        plan = StrategicPlan.from_dict({
            "plan_id": f"mock-{self.calls}",
            "state_id": state_id,
            "run_id": run_id,
            "current_objective": self.objective,
            "long_term_goal": "完成当前楼层并保留足够资源",
            "priority": ["survive", "preserve_resources", "advance"],
            "preferred_candidates": preferred,
            "avoid_candidates": [],
            "resource_constraints": {},
            "next_steps": ["执行当前合法候选", "状态变化后重新评估"],
            "replan_triggers": ["screen_change", "hp_change", "gold_change"],
            "reason": "离线战略大脑根据候选列表给出稳定的测试计划。",
            "uncertainty": "",
            "expires_after": 1,
        }, state_id=state_id, run_id=run_id)
        return BrainResponse(plan=plan, backend=self.backend_name, model="mock",
                             latency_ms=1, request_id=f"mock-{self.calls}")


class OpenAIStrategicClient:
    backend_name = "openai"
    # The live game must never wait on urllib/network I/O.  The orchestrator
    # uses this marker to run requests on its bounded background worker.
    async_required = True

    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 endpoint: str | None = None, timeout_ms: int = 6000,
                 max_output_tokens: int = 900, opener=None,
                 structured_output: bool | None = None,
                 reasoning_effort: str | None = None,
        provider_name: str = "openai", max_calls: int = 1) -> None:
        self.provider_name = provider_name or "openai"
        self.backend_name = self.provider_name
        self.api_key = api_key or os.environ.get("BRAIN_API_KEY", "").strip() \
            or os.environ.get("OPENAI_API_KEY", "").strip()
        # With no key, plan() returns a local fallback immediately. Running that
        # path on a daemon thread only creates lifecycle races during shutdown
        # and temporary-directory cleanup; real network calls remain async.
        self.async_required = bool(self.api_key)
        self.model = model or os.environ.get("BRAIN_MODEL", "").strip() \
            or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        self.endpoint = endpoint or os.environ.get(
            "BRAIN_ENDPOINT", ""
        ).strip() or os.environ.get(
            "OPENAI_BRAIN_ENDPOINT", "https://api.openai.com/v1/responses"
        )
        self.timeout_ms = max(500, int(timeout_ms))
        self.max_calls = max(1, int(max_calls))
        # Strategic plans are short, but 900 tokens is too small for some
        # gateways that emit JSON with long candidate/context strings. Keep a
        # bounded response while leaving enough room for a complete object.
        self.max_output_tokens = max(128, int(max_output_tokens))
        allowed_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        requested_effort = str(reasoning_effort or "").strip().lower()
        self.reasoning_effort = requested_effort if requested_effort in allowed_efforts else ""
        self.opener = opener or urllib.request.urlopen
        # Compatible gateways often reject vendor-specific json_schema output;
        # local StrategicPlan validation remains strict and is the default.
        if structured_output is None:
            structured_output = os.environ.get("OPENAI_STRUCTURED_OUTPUT", "0").lower() in {
                "1", "true", "yes", "on"
            }
        self.structured_output = bool(structured_output)

    def plan(self, payload: dict) -> BrainResponse:
        request_id = uuid.uuid4().hex[:16]
        if not self.api_key:
            return BrainResponse(backend=self.backend_name, model=self.model,
                                 request_id=request_id,
                                 error="未配置战略大脑 API Key（BRAIN_API_KEY/OPENAI_API_KEY）",
                                 error_kind="missing_key",
                                 fallback=True)
        started = time.monotonic()
        budget = current_budget()
        reserved_ms = budget.reserve(self.timeout_ms, minimum_ms=50) if budget else self.timeout_ms
        if not reserved_ms:
            return BrainResponse(backend=self.backend_name, model=self.model,
                                 request_id=request_id, error="战略决策预算已耗尽",
                                 error_kind="budget_exhausted",
                                 fallback=True)
        http_latency_ms = None
        parse_latency_ms = None
        parse_started = None
        response_bytes = None
        http_status = None
        try:
            body = self._request_body(payload)
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "jev-spire-brain/strategic",
                },
                method="POST",
            )
            http_started = time.monotonic()
            try:
                with self.opener(request, timeout=reserved_ms / 1000) as response:
                    raw = response.read(MAX_STRATEGIC_RESPONSE_BYTES + 1)
                http_latency_ms = int((time.monotonic() - http_started) * 1000)
            except urllib.error.HTTPError as exc:
                http_latency_ms = int((time.monotonic() - http_started) * 1000)
                http_status = int(exc.code)
                http_error_code, http_error_param = _http_error_details(exc)
                # Only the independently sanitized code/parameter may leave
                # the adapter; never copy the provider message or raw body.
                details = " ".join(
                    f"{name}={value}" for name, value in (
                        ("code", http_error_code), ("param", http_error_param))
                    if value is not None)
                return self._failure(
                    request_id, started,
                    f"HTTP {exc.code} from strategic provider"
                    + (f" ({details})" if details else ""),
                    error_kind=("auth" if exc.code in {401, 403} else
                                "rate_limit" if exc.code == 429 else "http"),
                    http_latency_ms=http_latency_ms,
                    http_status=http_status,
                    http_error_code=http_error_code,
                    http_error_param=http_error_param,
                )
            except (urllib.error.URLError, TimeoutError, OSError):
                http_latency_ms = int((time.monotonic() - http_started) * 1000)
                raise
            response_bytes = len(raw)
            if response_bytes > MAX_STRATEGIC_RESPONSE_BYTES:
                return self._failure(
                    request_id, started, "战略模型响应超过字节上限",
                    error_kind="response_too_large",
                    http_latency_ms=http_latency_ms,
                    response_bytes=response_bytes,
                )
            parse_started = time.monotonic()
            decoded = _unicode_safe_json(json.loads(raw.decode("utf-8", "replace")))
            texts = [_unicode_safe_json(item) for item in _extract_texts(decoded)]
            if not texts:
                raise ValueError("模型响应中没有可解析文本")
            parse_error: Exception | None = None
            plan = None
            for text in texts:
                if not isinstance(text, str):
                    continue
                if len(text.encode("utf-8", "replace")) > 64 * 1024:
                    parse_error = PlanValidationError("模型响应超过 64KB 限制")
                    continue
                for fragment in _json_candidates(text):
                    try:
                        data = _unwrap_plan(json.loads(fragment))
                        data = _coerce_steps_plan(
                            data,
                            state_id=str(payload.get("state_id", "")),
                            run_id=str(payload.get("run_id", "local")),
                            max_steps=int(payload.get("max_plan_steps", 5) or 5),
                        )
                        plan = StrategicPlan.from_dict(
                            data,
                            state_id=str(payload.get("state_id", "")),
                            run_id=str(payload.get("run_id", "local")),
                            max_steps=int(payload.get("max_plan_steps", 5) or 5),
                        )
                        break
                    except (UnicodeError, ValueError, KeyError, TypeError,
                            json.JSONDecodeError, PlanValidationError) as exc:
                        parse_error = exc
                if plan is not None:
                    break
            if plan is None:
                raise parse_error or ValueError("模型响应中没有有效战略计划")
            parse_latency_ms = int((time.monotonic() - parse_started) * 1000)
            usage = decoded.get("usage") if isinstance(decoded, dict) else {}
            return BrainResponse(
                plan=plan,
                backend=self.backend_name,
                model=str(decoded.get("model", self.model)),
                latency_ms=int((time.monotonic() - started) * 1000),
                request_id=request_id,
                usage=usage if isinstance(usage, dict) else {},
                http_latency_ms=http_latency_ms,
                parse_latency_ms=parse_latency_ms,
                response_bytes=response_bytes,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Exception text may include an endpoint with a query-string key.
            return self._failure(request_id, started,
                                 f"请求超时：{type(exc).__name__}" if isinstance(exc, TimeoutError)
                                 else f"网络错误：{type(exc).__name__}",
                                 error_kind="timeout" if isinstance(exc, TimeoutError) else "network",
                                 http_latency_ms=http_latency_ms,
                                 parse_latency_ms=(int((time.monotonic() - parse_started) * 1000)
                                                   if parse_started is not None else None),
                                 response_bytes=response_bytes)
        except UnicodeError as exc:
            return self._failure(request_id, started, f"计划解析编码失败：{type(exc).__name__}",
                                 error_kind="unicode_parse",
                                 http_latency_ms=http_latency_ms,
                                 parse_latency_ms=(int((time.monotonic() - parse_started) * 1000)
                                                   if parse_started is not None else None),
                                 response_bytes=response_bytes)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, PlanValidationError) as exc:
            return self._failure(request_id, started, redact_text(f"计划解析失败：{exc}"),
                                 error_kind="parse_or_validation",
                                 http_latency_ms=http_latency_ms,
                                 parse_latency_ms=(int((time.monotonic() - parse_started) * 1000)
                                                   if parse_started is not None else None),
                                 response_bytes=response_bytes)

    def _failure(self, request_id: str, started: float, error: str,
                 *, error_kind: str = "provider",
                 http_latency_ms: int | None = None,
                 parse_latency_ms: int | None = None,
                 response_bytes: int | None = None,
                 http_status: int | None = None,
                 http_error_code: str | None = None,
                 http_error_param: str | None = None) -> BrainResponse:
        return BrainResponse(
            backend=self.backend_name,
            model=self.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=request_id,
            error=error,
            error_kind=error_kind,
            fallback=True,
            http_latency_ms=http_latency_ms,
            parse_latency_ms=parse_latency_ms,
            response_bytes=response_bytes,
            http_status=http_status,
            http_error_code=http_error_code,
            http_error_param=http_error_param,
        )

    def _request_body(self, payload: dict) -> dict:
        """Build a request matching the configured endpoint's API protocol."""
        telemetry_only = {"strategy_fingerprint", "strategic_scope_id"}
        public_payload = {key: value for key, value in payload.items()
                          if not str(key).startswith("_internal_")
                          and key not in telemetry_only}
        user_text = json.dumps(public_payload, ensure_ascii=False, separators=(",", ":"))
        model_name = str(self.model or "").strip().lower()
        # Reasoning endpoints reject sampling controls such as temperature;
        # Kimi K3 has the opposite contract and requires its provider default.
        reasoning_model = bool(self.reasoning_effort) or model_name.startswith(
            ("gpt-5", "gpt-6", "o1", "o3", "o4"))
        temperature = 1 if model_name.startswith("kimi-k3") else 0.1
        system_text = SYSTEM_PROMPT + "\nJSON Schema:\n" + json.dumps(
            PLAN_SCHEMA, ensure_ascii=False, separators=(",", ":"))
        path = urlsplit(self.endpoint).path.rstrip("/").lower()
        if path.endswith("/responses"):
            body = {
                "model": self.model,
                "instructions": system_text,
                "input": user_text,
                "max_output_tokens": self.max_output_tokens,
                "text": {"format": {"type": "json_object"}},
            }
            if not reasoning_model:
                body["temperature"] = temperature
            if self.structured_output:
                body["text"] = {"format": {
                    "type": "json_schema",
                    "name": "strategic_plan",
                    "strict": False,
                    "schema": PLAN_SCHEMA,
                }}
            if self.reasoning_effort:
                body["reasoning"] = {"effort": self.reasoning_effort}
            return body

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": self.max_output_tokens,
        }
        if not reasoning_model:
            body["temperature"] = temperature
        # DeepSeek and several OpenAI-compatible mainland gateways support the
        # portable JSON-object mode even when they reject OpenAI's strict
        # json_schema extension. It prevents the truncated prose/empty-content
        # responses observed in live runs while keeping local schema validation
        # authoritative.
        json_model = model_name.startswith(("deepseek", "kimi", "qwen", "glm"))
        if self.structured_output:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "strategic_plan", "strict": True,
                                 "schema": PLAN_SCHEMA},
            }
        elif json_model:
            body["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body


class LoggingStrategicClient:
    """JSONL observability wrapper; credentials never enter the record."""

    _lock = threading.Lock()

    def __init__(self, inner, path: str | os.PathLike, event_sink=None) -> None:
        self.inner = inner
        self.backend_name = getattr(inner, "backend_name", "unknown")
        self.async_required = bool(
            getattr(inner, "async_required", str(self.backend_name).lower() in {"openai", "gpt"})
        )
        self.path = Path(path)
        self.event_sink = event_sink
        self._decision_request_ids: dict[str, list[str]] = {}
        self._request_ids_lock = threading.Lock()

    def request_ids_for_decision(self, decision_id: str) -> list[str]:
        with self._request_ids_lock:
            return list(self._decision_request_ids.get(str(decision_id or ""), ()))

    def _remember_request(self, decision_id: str, request_id: str) -> None:
        if not decision_id:
            return
        with self._request_ids_lock:
            values = self._decision_request_ids.setdefault(decision_id, [])
            if request_id not in values:
                values.append(request_id)
            if len(values) > 32:
                del values[:-32]
            while len(self._decision_request_ids) > 128:
                self._decision_request_ids.pop(next(iter(self._decision_request_ids)))

    def plan(self, payload: dict) -> BrainResponse:
        started = time.monotonic()
        request_id = str(payload.get("request_id") or uuid.uuid4())
        identity = {key: payload.get(key) for key in
                    ("run_id", "run_epoch", "decision_id", "state_id", "plan_id",
                     "config_id", "generation", "trigger", "strategy_fingerprint",
                     "strategic_scope_id")}
        identity["request_id"] = request_id
        identity["logical_request_id"] = str(payload.get("logical_request_id") or request_id)
        identity["attempt_id"] = str(payload.get("attempt_id") or f"{request_id}:1")
        knowledge = payload.get("knowledge_context")
        if isinstance(knowledge, dict):
            identity["knowledge_citation_ids"] = [str(value) for value in
                                                   (knowledge.get("citation_ids") or [])][:8]
        self._remember_request(str(identity.get("decision_id") or ""), request_id)
        internal_timing = payload.get("_internal_timing")
        internal_timing = internal_timing if isinstance(internal_timing, dict) else {}
        duration_fields = ("queue_wait_ms", "pre_provider_ms")
        timing_fields = {
            key: max(0, int(internal_timing[key]))
            for key in duration_fields
            if isinstance(internal_timing.get(key), (int, float))
            and not isinstance(internal_timing.get(key), bool)
        }
        if self.event_sink is not None:
            try:
                self.event_sink("provider_request", {**identity, "provider": self.backend_name,
                    "model": getattr(self.inner, "model", ""),
                    **timing_fields,
                    "state_blob": payload.get("state"),
                    "candidate_blob": payload.get("candidates"),
                    "knowledge_citation_ids": identity.get("knowledge_citation_ids", [])})
            except Exception:
                pass
        try:
            result = self.inner.plan(payload)
        except Exception as exc:  # keep the wrapper transparent to the caller
            result = BrainResponse(backend=self.backend_name,
                                   error=redact_text(f"{type(exc).__name__}: {exc}"), fallback=True)
        provider_request_id = result.request_id
        # The wrapper owns the cross-provider correlation ID. Preserve the
        # provider's opaque ID only in the legacy log.
        result.request_id = request_id
        worker_elapsed_ms = int((time.monotonic() - started) * 1000) + timing_fields.get(
            "pre_provider_ms", 0)
        plan = result.plan.model_dict() if result.plan is not None else None
        record = redact_value({
            "ts": time.time(),
            "backend": result.backend or self.backend_name,
            "model": result.model,
            "request_id": result.request_id,
            "latency_ms": result.latency_ms or int((time.monotonic() - started) * 1000),
            "http_latency_ms": result.http_latency_ms,
            "parse_latency_ms": result.parse_latency_ms,
            "response_bytes": result.response_bytes,
            "http_status": result.http_status,
            "http_error_code": result.http_error_code,
            "http_error_param": result.http_error_param,
            "queue_wait_ms": timing_fields.get("queue_wait_ms"),
            "pre_provider_ms": timing_fields.get("pre_provider_ms"),
            "worker_elapsed_ms": worker_elapsed_ms,
            "usage": result.usage,
            "error": result.error,
            "error_kind": result.error_kind,
            "fallback": bool(result.fallback),
            "state_id": payload.get("state_id", ""),
            "run_id": payload.get("run_id", ""),
            "trigger": payload.get("trigger", ""),
            "plan": plan,
            "knowledge_citation_ids": identity.get("knowledge_citation_ids", []),
        })
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            append_bounded(self.path,
                           json.dumps(record, ensure_ascii=False, default=str) + "\n",
                           lock=self._lock)
        except (OSError, TypeError, ValueError):
            pass
        if self.event_sink is not None:
            try:
                self.event_sink("provider_response", {**identity,
                    "request_id": result.request_id, "provider_request_id": provider_request_id,
                    "provider": result.backend or self.backend_name,
                    "model": result.model, "latency_ms": record["latency_ms"],
                    "http_latency_ms": result.http_latency_ms,
                    "parse_latency_ms": result.parse_latency_ms,
                    "response_bytes": result.response_bytes,
                    "http_status": result.http_status,
                    "http_error_code": result.http_error_code,
                    "http_error_param": result.http_error_param,
                    "queue_wait_ms": timing_fields.get("queue_wait_ms"),
                    "pre_provider_ms": timing_fields.get("pre_provider_ms"),
                    "worker_elapsed_ms": worker_elapsed_ms,
                    "usage": result.usage, "error": result.error, "error_kind": result.error_kind,
                    "fallback": bool(result.fallback),
                    "response_blob": plan,
                    "knowledge_citation_ids": identity.get("knowledge_citation_ids", [])})
            except Exception:
                pass
        return result


def get_strategic_brain(backend: str | None = None, **kwargs):
    """Build a provider without making a network request."""
    selected = (backend or os.environ.get("BRAIN_BACKEND") or "openai").lower()
    if selected in {"none", "disabled", "off", "jev", "legacy"}:
        return UnavailableStrategicClient("战略大脑已禁用",
                                          backend_name="jev" if selected in {"jev", "legacy"}
                                          else "disabled")
    if selected == "mock":
        return MockStrategicClient(**{k: v for k, v in kwargs.items() if k in {"preferred", "objective"}})
    compatible = {
        "openai", "gpt", "deepseek", "qwen", "tongyi", "zhipu", "glm",
        "moonshot", "kimi", "siliconflow", "doubao", "openai-compatible",
        "openai_compatible", "compatible", "local", "vllm", "ollama",
        "hiyo-codex",
    }
    if selected in compatible:
        return OpenAIStrategicClient(
            provider_name=selected,
            **kwargs,
        )
    return UnavailableStrategicClient(f"未知战略大脑后端：{selected}", backend_name=selected)
